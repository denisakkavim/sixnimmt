"""Exercise the real SDK-backed stdio bridge without invoking an LLM."""

import json
import select
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from sixnimmt.arena.bots.external_harnesses.mcp import MCP_PROTOCOL_VERSION, game_tools
from sixnimmt.arena.bots.external_harnesses.protocol import HarnessError
from sixnimmt.arena.bots.external_harnesses.transport import ControllerServer


class MCPSeat:
    session_id = "seat-mcp"
    credential = "private-seat-credential"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def get_game_info(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "rules": "sixnimmt rules"}

    def play(
        self, session_id: str, proposal: dict[str, Any] | None = None, cancel: threading.Event | None = None
    ) -> dict[str, Any]:
        self.started.set()
        if proposal is not None:
            return {"status": "terminal", "result": {"reason": "finished"}}
        assert cancel is not None
        assert cancel.wait(10)
        self.cancelled.set()
        raise HarnessError("cancelled", "The tool call was cancelled.")


class WirePeer:
    def __init__(self, endpoint: str, seat: MCPSeat, directory: Path) -> None:
        self.seat = seat
        self.process = subprocess.Popen(
            [sys.executable, "-m", "sixnimmt.arena.bots.external_harnesses.mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            cwd=directory,
            env={"SIXNIMMT_ENDPOINT": endpoint, "SIXNIMMT_CREDENTIAL": seat.credential},
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
        readable, _, _ = select.select([self.process.stdout], [], [], 5)
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
def peer(tmp_path: Path) -> Iterator[WirePeer]:
    seat = MCPSeat()
    with ControllerServer([seat]) as controller:
        wire = WirePeer(controller.endpoint, seat, tmp_path)
        try:
            yield wire
        finally:
            wire.close()


def test_discovers_only_the_pinned_modern_revision_without_initializing(peer: WirePeer) -> None:
    peer.request(1, "server/discover")
    response = peer.receive()
    assert response["id"] == 1
    assert response["result"]["resultType"] == "complete"
    assert response["result"]["supportedVersions"] == [MCP_PROTOCOL_VERSION]
    assert set(response["result"]["capabilities"]) == {"tools"}


def test_exposes_only_the_two_game_tools(peer: WirePeer) -> None:
    peer.request(1, "tools/list")
    response = peer.receive()
    assert [tool["name"] for tool in response["result"]["tools"]] == ["get_game_info", "play"]
    assert response["result"]["resultType"] == "complete"


def test_tool_schema_resolves_nested_proposal_definitions() -> None:
    schema = game_tools()[1].input_schema
    validator = Draft202012Validator(schema)
    validator.check_schema(schema)
    validator.validate({
        "session_id": "seat-mcp",
        "proposal": {
            "protocol_version": 1,
            "session_id": "seat-mcp",
            "decision_id": "decision-1",
            "submission_id": "submission-1",
            "view_id": "view-1",
            "actions": [{"type": "select_card", "card": 42}],
            "memory": None,
        },
    })


def test_reads_authorized_game_info_as_structured_and_text_content(peer: WirePeer) -> None:
    peer.request(1, "tools/call", {"name": "get_game_info", "arguments": {"session_id": peer.seat.session_id}})
    response = peer.receive()["result"]
    expected = {"session_id": peer.seat.session_id, "rules": "sixnimmt rules"}
    assert response["structuredContent"] == expected
    assert json.loads(response["content"][0]["text"]) == expected
    assert response["resultType"] == "complete"
    assert response.get("isError", False) is False


def test_forwards_a_proposal_and_returns_the_terminal_result(peer: WirePeer) -> None:
    peer.request(1, "tools/call", {"name": "play", "arguments": {"session_id": peer.seat.session_id, "proposal": {}}})
    response = peer.receive()["result"]
    assert response["structuredContent"]["status"] == "terminal"


def test_rejects_legacy_initialization_and_can_then_accept_a_modern_request(peer: WirePeer) -> None:
    peer.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
    assert peer.receive()["error"]["code"] == -32022
    peer.request(2, "server/discover")
    assert peer.receive()["result"]["supportedVersions"] == [MCP_PROTOCOL_VERSION]


@pytest.mark.parametrize("meta", [None, {}, {"io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION}])
def test_requires_the_metadata_envelope_on_every_request(peer: WirePeer, meta: dict[str, Any] | None) -> None:
    peer.request(1, "server/discover")
    peer.receive()
    peer.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": meta}})
    assert peer.receive()["error"]["code"] == -32602


def test_rejects_unsupported_revision_with_the_supported_revision(peer: WirePeer) -> None:
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


def test_reports_cross_seat_access_as_a_safe_tool_error(peer: WirePeer) -> None:
    peer.request(1, "tools/call", {"name": "get_game_info", "arguments": {"session_id": "another-seat"}})
    result = peer.receive()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "unauthorized"
    assert peer.seat.credential not in json.dumps(result)


def test_cancellation_releases_play_without_closing_the_mcp_connection(peer: WirePeer) -> None:
    peer.request(1, "tools/call", {"name": "play", "arguments": {"session_id": peer.seat.session_id}})
    assert peer.seat.started.wait(3)
    peer.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
    assert peer.seat.cancelled.wait(3)
    peer.request(2, "tools/list")
    assert peer.receive()["id"] == 2


def test_closing_stdin_cancels_pending_play_and_exits(peer: WirePeer) -> None:
    peer.request(1, "tools/call", {"name": "play", "arguments": {"session_id": peer.seat.session_id}})
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
def test_rejects_ambiguous_or_malformed_json(peer: WirePeer, payload: bytes, error_code: int) -> None:
    assert peer.process.stdin is not None
    peer.process.stdin.write(payload)
    peer.process.stdin.flush()
    assert peer.receive()["error"]["code"] == error_code
