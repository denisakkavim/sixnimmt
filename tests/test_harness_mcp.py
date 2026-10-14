"""Exercise the real SDK-backed stdio bridge without invoking an LLM."""

import json
import select
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
import pytest
from jsonschema import Draft202012Validator
from mcp.shared.message import SessionMessage

from sixnimmt.arena.bots.agent_protocol import play_tool
from sixnimmt.arena.bots.external_harnesses.mcp import MCP_PROTOCOL_VERSION, _Bridge, _schema_key, game_tools
from sixnimmt.arena.bots.external_harnesses.messages import DecisionReply, GameInfo, PlayReply, TerminalReply
from sixnimmt.arena.bots.external_harnesses.protocol import HarnessError
from sixnimmt.arena.bots.external_harnesses.transport import ControllerServer, SeatClient, SeatEndpoint
from tests.test_harness import RunningTable


class MCPSeat:
    session_id = "seat-mcp"
    credential = "private-seat-credential"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def get_game_info(self, session_id: str) -> GameInfo:
        return {
            "protocol_version": 1,
            "session_id": session_id,
            "player_id": "a",
            "name": "A",
            "rules": {},
            "protocol": {},
            "instructions": "sixnimmt rules",
            "proposal_schema": {},
            "memory_enabled": False,
            "memory_max_chars": 4000,
        }

    def get_agent_info(self, session_id: str) -> GameInfo:
        return self.get_game_info(session_id)

    def get_play_tool(self, session_id: str) -> dict[str, Any]:
        return dict(play_tool(None, memory_enabled=False, memory_max_chars=4000)["function"])

    def fail_schema_refresh(self, session_id: str) -> dict[str, Any]:
        return {"code": "schema_refresh_required", "message": "Refresh is required"}

    def play_submission(
        self, session_id: str, proposal: dict[str, Any] | None = None, cancel: threading.Event | None = None
    ) -> PlayReply:
        return self.play(session_id, proposal, cancel)

    def play(
        self, session_id: str, proposal: dict[str, Any] | None = None, cancel: threading.Event | None = None
    ) -> TerminalReply:
        self.started.set()
        if proposal is not None:
            return {
                "status": "terminal",
                "offer": None,
                "receipt": None,
                "result": {
                    "outcome": "finished",
                    "reason": "finished",
                    "winners": [],
                    "scores": {},
                    "final_view": None,
                },
            }
        assert cancel is not None
        assert cancel.wait(10)
        self.cancelled.set()
        raise HarnessError("cancelled", "The tool call was cancelled.")


class WirePeer[SeatType: SeatEndpoint]:
    def __init__(self, endpoint: str, seat: SeatType, directory: Path, schema_mode: str = "dynamic") -> None:
        self.seat = seat
        self.process = subprocess.Popen(
            [sys.executable, "-m", "sixnimmt.arena.bots.external_harnesses.mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            cwd=directory,
            env={
                "SIXNIMMT_ENDPOINT": endpoint,
                "SIXNIMMT_CREDENTIAL": seat.credential,
                "SIXNIMMT_SESSION_ID": seat.session_id,
                "SIXNIMMT_SCHEMA_MODE": schema_mode,
            },
        )

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def request(self, request_id: int, method: str, params: dict[str, Any] | None = None) -> None:
        arguments = {} if params is None else dict(params)
        arguments["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": arguments})

    def receive(self) -> dict[str, Any]:
        assert self.process.stdout is not None
        readable, _, _ = select.select([self.process.stdout], [], [], 10)
        assert len(readable) > 0, "The MCP bridge did not respond"
        line = self.process.stdout.readline()
        if line == b"":
            assert self.process.stderr is not None
            pytest.fail(f"The MCP bridge exited: {self.process.stderr.read().decode()}")
        return json.loads(line)

    def close(self) -> None:
        assert self.process.stdin is not None
        if not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            pytest.fail("The MCP bridge did not exit after stdin closed")
        finally:
            assert self.process.stdout is not None
            assert self.process.stderr is not None
            self.process.stdout.close()
            self.process.stderr.close()


@pytest.fixture
def peer(tmp_path: Path) -> Iterator[WirePeer[MCPSeat]]:
    seat = MCPSeat()
    with ControllerServer([seat]) as controller:
        wire = WirePeer(controller.endpoint, seat, tmp_path)
        try:
            yield wire
        finally:
            wire.close()


def test_discovers_only_the_pinned_modern_revision_without_initializing(peer: WirePeer[MCPSeat]) -> None:
    peer.request(1, "server/discover")
    response = peer.receive()
    assert response["id"] == 1
    assert response["result"]["resultType"] == "complete"
    assert response["result"]["supportedVersions"] == [MCP_PROTOCOL_VERSION]
    assert set(response["result"]["capabilities"]) == {"tools"}


def test_exposes_only_the_two_game_tools(peer: WirePeer[MCPSeat]) -> None:
    peer.request(1, "tools/list")
    response = peer.receive()
    assert [tool["name"] for tool in response["result"]["tools"]] == ["get_game_info", "play"]
    assert response["result"]["resultType"] == "complete"


def test_tool_schema_resolves_nested_proposal_definitions() -> None:
    schema = game_tools()[1].input_schema
    validator = Draft202012Validator(schema)
    validator.check_schema(schema)
    validator.validate({
        "proposal": {
            "decision_id": "decision-1",
            "submission_id": "submission-1",
            "actions": [{"type": "select_card", "card": 42}],
            "memory": None,
            "explanation": None,
        },
    })


def test_reads_authorized_game_info_as_structured_and_text_content(peer: WirePeer[MCPSeat]) -> None:
    peer.request(1, "tools/call", {"name": "get_game_info", "arguments": {}})
    response = peer.receive()["result"]
    expected = peer.seat.get_game_info(peer.seat.session_id)
    assert response["structuredContent"] == expected
    assert json.loads(response["content"][0]["text"]) == expected
    assert response["resultType"] == "complete"
    assert response.get("isError", False) is False


def test_forwards_a_proposal_and_returns_the_terminal_result(peer: WirePeer[MCPSeat]) -> None:
    peer.request(
        1,
        "tools/call",
        {
            "name": "play",
            "arguments": {
                "proposal": {
                    "decision_id": "d1",
                    "submission_id": "s1",
                    "actions": [{"type": "select_card", "card": 42}],
                    "memory": None,
                    "explanation": None,
                }
            },
        },
    )
    response = peer.receive()["result"]
    assert response["structuredContent"]["status"] == "terminal"


def test_rejects_legacy_initialization_and_can_then_accept_a_modern_request(peer: WirePeer[MCPSeat]) -> None:
    peer.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
    assert peer.receive()["error"]["code"] == -32022
    peer.request(2, "server/discover")
    assert peer.receive()["result"]["supportedVersions"] == [MCP_PROTOCOL_VERSION]


@pytest.mark.parametrize("meta", [None, {}, {"io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION}])
def test_requires_the_metadata_envelope_on_every_request(peer: WirePeer[MCPSeat], meta: dict[str, Any] | None) -> None:
    peer.request(1, "server/discover")
    peer.receive()
    peer.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": meta}})
    assert peer.receive()["error"]["code"] == -32602


def test_rejects_unsupported_revision_with_the_supported_revision(peer: WirePeer[MCPSeat]) -> None:
    peer.send({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2099-01-01",
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    })
    response = peer.receive()
    assert response["error"]["code"] == -32022
    assert response["error"]["data"] == {"supported": [MCP_PROTOCOL_VERSION], "requested": "2099-01-01"}


def test_reports_cross_seat_access_as_a_safe_tool_error(peer: WirePeer[MCPSeat]) -> None:
    peer.request(1, "tools/call", {"name": "get_game_info", "arguments": {"session_id": "another-seat"}})
    result = peer.receive()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_request"
    assert peer.seat.credential not in json.dumps(result)


def test_cancellation_releases_play_without_closing_the_mcp_connection(peer: WirePeer[MCPSeat]) -> None:
    peer.request(1, "tools/call", {"name": "play", "arguments": {"proposal": None}})
    assert peer.seat.started.wait(3)
    peer.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
    assert peer.seat.cancelled.wait(3)
    peer.request(2, "tools/list")
    assert peer.receive()["id"] == 2


def test_closing_stdin_cancels_pending_play_and_exits(peer: WirePeer[MCPSeat]) -> None:
    peer.request(1, "tools/call", {"name": "play", "arguments": {"proposal": None}})
    assert peer.seat.started.wait(3)
    assert peer.process.stdin is not None
    peer.process.stdin.close()
    assert peer.process.wait(timeout=3) == 0
    assert peer.seat.cancelled.wait(3)


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        (b'{"id":1,"id":2}\n', -32700),
        (b'{"number":NaN}\n', -32700),
        (b'{"number":1e999}\n', -32700),
        (b"[" * 2000 + b"]" * 2000 + b"\n", -32600),
        (b"not JSON\n", -32700),
    ],
    ids=["duplicate-fields", "nan", "overflow", "deep-array", "malformed"],
)
def test_rejects_ambiguous_or_malformed_json(peer: WirePeer[MCPSeat], payload: bytes, error_code: int) -> None:
    assert peer.process.stdin is not None
    peer.process.stdin.write(payload)
    peer.process.stdin.flush()
    assert peer.receive()["error"]["code"] == error_code


class ChangingSeat(MCPSeat):
    def __init__(self) -> None:
        super().__init__()
        self.hand = [1, 2]
        self.failed = threading.Event()

    def get_play_tool(self, session_id: str) -> dict[str, Any]:
        function = super().get_play_tool(session_id)
        proposal = function["parameters"]["properties"]["proposal"]["anyOf"][0]
        alternatives = proposal["properties"]["actions"]["items"]["anyOf"]
        for action in alternatives:
            if action["properties"]["type"]["const"] == "select_card":
                action["properties"]["card"]["enum"] = list(self.hand)
        return function

    def play_submission(
        self, session_id: str, proposal: dict[str, Any] | None = None, cancel: threading.Event | None = None
    ) -> DecisionReply:
        self.hand = [2]
        self.started.set()
        return {
            "status": "decision",
            "receipt": None,
            "result": None,
            "offer": {
                "protocol_version": 1,
                "session_id": session_id,
                "decision_id": "decision-2",
                "view_id": "view-2",
                "view": {},
                "observation": "Hand: 2",
                "instructions": "Play the remaining card.",
                "action_tools": [],
                "rejection": None,
                "memory": None,
                "deadline": None,
            },
        }

    def fail_schema_refresh(self, session_id: str) -> dict[str, Any]:
        self.failed.set()
        return super().fail_schema_refresh(session_id)


def card_schema(response: dict[str, Any]) -> dict[str, Any]:
    schema = response["result"]["tools"][1]["inputSchema"]
    alternatives = schema["properties"]["proposal"]["anyOf"][0]["properties"]["actions"]["items"]["anyOf"]
    return next(
        action["properties"]["card"]
        for action in alternatives
        if action["properties"]["type"]["const"] == "select_card"
    )


def test_updated_tool_schema_is_sent_before_the_next_decision(tmp_path: Path) -> None:
    seat = ChangingSeat()
    with ControllerServer([seat]) as controller:
        wire = WirePeer(controller.endpoint, seat, tmp_path)
        try:
            wire.request(1, "subscriptions/listen", {"notifications": {"toolsListChanged": True}})
            acknowledged = wire.receive()
            assert acknowledged["method"] == "notifications/subscriptions/acknowledged"
            assert acknowledged["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"] == 1
            wire.request(2, "tools/list")
            assert card_schema(wire.receive())["enum"] == [1, 2]
            wire.request(3, "tools/call", {"name": "play", "arguments": {"proposal": None}})
            changed = wire.receive()
            assert changed["method"] == "notifications/tools/list_changed"
            assert changed["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"] == 1
            wire.request(4, "tools/list")
            refreshed = wire.receive()
            assert refreshed["id"] == 4
            assert card_schema(refreshed)["enum"] == [2]
            decision = wire.receive()
            assert decision["id"] == 3
            assert decision["result"]["structuredContent"]["status"] == "decision"
            assert not seat.failed.is_set()
        finally:
            wire.close()


def test_dynamic_mode_fails_explicitly_when_the_client_does_not_refresh(tmp_path: Path) -> None:
    seat = ChangingSeat()
    with ControllerServer([seat]) as controller:
        wire = WirePeer(controller.endpoint, seat, tmp_path)
        try:
            wire.request(1, "tools/list")
            wire.receive()
            wire.request(2, "tools/call", {"name": "play", "arguments": {"proposal": None}})
            result = wire.receive()["result"]
            assert result["isError"] is True
            assert result["structuredContent"]["error"]["code"] == "schema_refresh_required"
            assert seat.failed.is_set()
        finally:
            wire.close()


def test_static_mode_delivers_decisions_without_requiring_schema_refresh(tmp_path: Path) -> None:
    seat = ChangingSeat()
    with ControllerServer([seat]) as controller:
        wire = WirePeer(controller.endpoint, seat, tmp_path, schema_mode="static")
        try:
            wire.request(1, "tools/list")
            initial = wire.receive()
            assert "enum" not in card_schema(initial)
            wire.request(2, "tools/call", {"name": "play", "arguments": {"proposal": None}})
            decision = wire.receive()
            assert decision["id"] == 2
            assert decision["result"]["structuredContent"]["status"] == "decision"
            wire.request(3, "tools/list")
            assert wire.receive()["result"]["tools"] == initial["result"]["tools"]
            assert not seat.failed.is_set()
        finally:
            wire.close()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class RefreshClient(SeatClient):
    """Control transport responses while testing concurrent delivery barriers."""

    def __init__(self) -> None:
        super().__init__("tcp://127.0.0.1:1", "offline-test")
        self.failed = False

    def get_play_tool(self, session_id: str, cancel: threading.Event | None = None) -> dict[str, Any]:
        return dict(play_tool(None, memory_enabled=False, memory_max_chars=4000)["function"])

    def fail_schema_refresh(self, session_id: str, cancel: threading.Event | None = None) -> dict[str, Any]:
        self.failed = True
        return {"code": "schema_refresh_required", "message": "Refresh is required"}


async def wait_for_refresh_waiters(bridge: _Bridge, count: int) -> None:
    with anyio.fail_after(1):
        while sum(refresh.waiters for refresh in bridge.refresh_waiters.values()) != count:
            await anyio.sleep(0)


def send_refreshed_schema(bridge: _Bridge, client: RefreshClient) -> None:
    bridge.list_responses[1] = _schema_key(client.get_play_tool("seat"))
    bridge.response_sent(SessionMessage(types.JSONRPCResponse(jsonrpc="2.0", id=1, result={})))


@pytest.mark.anyio
async def test_one_schema_refresh_releases_concurrent_deliveries() -> None:
    client = RefreshClient()
    bridge = _Bridge(client, "seat")
    with anyio.fail_after(1):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(bridge.refresh_before_delivery, threading.Event())
            await wait_for_refresh_waiters(bridge, 1)
            tasks.start_soon(bridge.refresh_before_delivery, threading.Event())
            await wait_for_refresh_waiters(bridge, 2)
            send_refreshed_schema(bridge, client)
    assert not client.failed
    assert bridge.refresh_waiters == {}


async def cancellable_refresh(bridge: _Bridge, scope: anyio.CancelScope, finished: anyio.Event) -> None:
    with scope:
        await bridge.refresh_before_delivery(threading.Event())
    finished.set()


@pytest.mark.anyio
async def test_cancelling_one_delivery_preserves_another_deliverys_refresh_wait() -> None:
    client = RefreshClient()
    bridge = _Bridge(client, "seat")
    cancelled = anyio.Event()
    scope = anyio.CancelScope()
    with anyio.fail_after(1):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(cancellable_refresh, bridge, scope, cancelled)
            await wait_for_refresh_waiters(bridge, 1)
            tasks.start_soon(bridge.refresh_before_delivery, threading.Event())
            await wait_for_refresh_waiters(bridge, 2)
            scope.cancel()
            await cancelled.wait()
            await wait_for_refresh_waiters(bridge, 1)
            send_refreshed_schema(bridge, client)
    assert not client.failed
    assert bridge.refresh_waiters == {}


def test_malformed_mcp_proposals_count_toward_the_brokers_protocol_error_limit(tmp_path: Path) -> None:
    table = RunningTable(decision_timeout=30)
    try:
        with ControllerServer([table.a]) as controller:
            wire = WirePeer(controller.endpoint, table.a, tmp_path, schema_mode="static")
            try:
                wire.request(1, "tools/call", {"name": "play", "arguments": {"proposal": None}})
                assert wire.receive()["result"]["structuredContent"]["status"] == "decision"
                for request_id in range(2, 10):
                    wire.request(request_id, "tools/call", {"name": "play", "arguments": {"proposal": {}}})
                    response = wire.receive()["result"]
                    assert response["isError"] is True
                    assert response["structuredContent"]["error"]["code"] == "invalid_proposal"
                wire.request(10, "tools/call", {"name": "play", "arguments": {"proposal": None}})
                terminal = wire.receive()["result"]["structuredContent"]
                assert terminal["status"] == "terminal"
                assert terminal["result"]["outcome"] == "failed"
                assert table.a.stats()["submissions"] == 0
            finally:
                wire.close()
    finally:
        table.close()
