"""Opt-in installed-client checks against a local fake provider and the real MCP bridge.

Run with SIXNIMMT_TEST_HARNESS_CLIENTS=1. These tests never call a model provider:
model responses are deterministic SSE fixtures served exclusively on loopback.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from typing import Any, Literal, cast

import pytest
from jsonschema import Draft202012Validator

from sixnimmt.arena.bots.external import HarnessBot
from sixnimmt.arena.bots.external_harnesses.broker import SeatSession
from sixnimmt.arena.bots.external_harnesses.connection import codex_mcp_setting, connection_environment
from sixnimmt.arena.bots.external_harnesses.transport import ControllerServer
from sixnimmt.arena.bots.heuristics import LowestFittingCardBot
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.match import run_match
from sixnimmt.arena.results import MatchOutcome
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.state import PlayerSeat
from sixnimmt.engine.views import MatchView

pytestmark = pytest.mark.skipif(
    os.environ.get("SIXNIMMT_TEST_HARNESS_CLIENTS") != "1",
    reason="Set SIXNIMMT_TEST_HARNESS_CLIENTS=1 to run installed CLIs against the offline provider",
)


def _objects(value: object) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)
    elif isinstance(value, str):
        if "\nOutput:\n" in value:
            value = value.split("\nOutput:\n", 1)[1]
        if not value.startswith(("{", "[")):
            return
        try:
            decoded, _ = json.JSONDecoder().raw_decode(value)
        except ValueError:
            return
        yield from _objects(decoded)


def _event(name: str, payload: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()


class OfflineProvider(ThreadingHTTPServer):
    def __init__(self, kind: Literal["codex", "claude"], directory: Path) -> None:
        self.kind = kind
        self.directory = directory
        self.requests: list[dict[str, Any]] = []
        self.decisions: list[tuple[dict[str, Any], MatchView]] = []
        self.errors: list[str] = []
        self.terminal_seen = False
        super().__init__(("127.0.0.1", 0), OfflineHandler)

    def next_call(self, request: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        if len(self.requests) > 25:
            self.errors.append("The harness did not finish within 25 offline requests")
            return "", None
        replies = [item for item in _objects(request) if item.get("status") in ("decision", "terminal")]
        if len(replies) == 0:
            return "play", {"proposal": None}
        reply = replies[-1]
        if reply["status"] == "terminal":
            assert reply["result"]["outcome"] == "finished", reply
            self.terminal_seen = True
            return "", None
        offer = reply["offer"]
        view = MatchView.model_validate(offer["view"])
        tool = next(
            item for item in _objects(request.get("tools", [])) if item.get("name") in ("play", "mcp__sixnimmt__play")
        )
        schema = tool["parameters"] if self.kind == "codex" else tool["input_schema"]
        self.decisions.append((schema, view))
        action = LowestFittingCardBot().act(view)
        arguments = {
            "proposal": {
                "decision_id": offer["decision_id"],
                "submission_id": f"offline-{len(self.decisions)}",
                "actions": [
                    action.model_dump(mode="json", exclude={"action_id", "from_view", "expected_view_version"})
                ],
                "memory": None,
                "explanation": "Deterministic offline provider fixture.",
            }
        }
        Draft202012Validator(schema).validate(arguments)
        return "play", arguments


class OfflineHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib override parameter.
        pass

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"models":[]}')

    def do_POST(self) -> None:
        provider = cast(OfflineProvider, self.server)
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        if "count_tokens" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"input_tokens":100}')
            return
        try:
            request = json.loads(body)
            index = len(provider.requests)
            provider.requests.append(request)
            (provider.directory / f"request-{index}.json").write_text(json.dumps(request, indent=2))
            name, arguments = provider.next_call(request)
        except Exception as error:
            provider.errors.append(repr(error))
            self.send_error(500, "Offline fixture could not interpret the request")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if provider.kind == "codex":
            self._codex(index, name, arguments)
        else:
            self._claude(index, name, arguments)
        self.wfile.flush()

    def _codex(self, index: int, name: str, arguments: dict[str, Any] | None) -> None:
        response: dict[str, Any] = {
            "id": f"resp_{index}",
            "object": "response",
            "created_at": 0,
            "model": "offline-probe",
            "status": "in_progress",
            "output": [],
        }
        self.wfile.write(_event("response.created", {"type": "response.created", "response": response}))
        if arguments is None:
            item = {
                "type": "message",
                "id": f"msg_{index}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Offline game complete."}],
            }
        else:
            item = {
                "type": "function_call",
                "id": f"fc_{index}",
                "call_id": f"call_{index}",
                "namespace": "mcp__sixnimmt",
                "name": name,
                "arguments": json.dumps(arguments),
                "status": "completed",
            }
        for event in ("response.output_item.added", "response.output_item.done"):
            self.wfile.write(_event(event, {"type": event, "output_index": 0, "item": item}))
        response.update(
            status="completed", output=[item], usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        )
        self.wfile.write(_event("response.completed", {"type": "response.completed", "response": response}))

    def _claude(self, index: int, name: str, arguments: dict[str, Any] | None) -> None:
        message = {
            "id": f"msg_{index}",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        self.wfile.write(_event("message_start", {"type": "message_start", "message": message}))
        if arguments is None:
            block = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": "Offline game complete."}
            reason = "end_turn"
        else:
            block = {"type": "tool_use", "id": f"tool_{index}", "name": f"mcp__sixnimmt__{name}", "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(arguments)}
            reason = "tool_use"
        self.wfile.write(
            _event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": block})
        )
        self.wfile.write(_event("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}))
        self.wfile.write(_event("content_block_stop", {"type": "content_block_stop", "index": 0}))
        self.wfile.write(
            _event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": reason, "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
            )
        )
        self.wfile.write(_event("message_stop", {"type": "message_stop"}))


def _command(
    kind: str, binary: str, provider: OfflineProvider, session: SeatSession, endpoint: str
) -> tuple[list[str], dict[str, str]]:
    base_url = f"http://127.0.0.1:{provider.server_port}"
    # Credentials and desktop-injected options must not reach this isolated fixture.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CODEX_", "ANTHROPIC_", "OPENAI_", "CLAUDE_"))
    }
    environment.update(
        connection_environment(session, endpoint, schema_mode="static" if kind == "codex" else "dynamic")
    )
    if kind == "codex":
        return [
            binary,
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--ephemeral",
            "--json",
            "--model",
            "offline-probe",
            "--enable",
            "mcp_2026_07_28",
            "--disable",
            "apps",
            "-c",
            'model_provider="offline"',
            "-c",
            'model_reasoning_effort="medium"',
            "-c",
            f'model_providers.offline={{name="offline",base_url="{base_url}/v1",wire_api="responses",requires_openai_auth=false}}',
            "-c",
            codex_mcp_setting(session.wait_timeout_seconds, approve_game_tools=True),
            "-",
        ], environment
    environment.update({
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": "offline-dummy-key",
        "CLAUDE_CONFIG_DIR": str(provider.directory / "claude-config"),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ENABLE_TOOL_SEARCH": "false",
        "MCP_SDK_GENERATION": "v2",
        "MCP_PROTOCOL_NEGOTIATION": "auto",
        "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS": "0",
    })
    bridge = {
        "command": sys.executable,
        "args": ["-m", "sixnimmt.arena.bots.external_harnesses.mcp"],
        "env": connection_environment(session, endpoint),
    }
    return [
        binary,
        "-p",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "claude-sonnet-4-5",
        "--effort",
        "medium",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        json.dumps({"mcpServers": {"sixnimmt": bridge}}),
        "--allowedTools",
        "mcp__sixnimmt__play",
        "--tools",
        "",
    ], environment


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_installed_harness_completes_game_with_expected_model_facing_schema(
    tmp_path: Path, kind: Literal["codex", "claude"]
) -> None:
    binary = shutil.which(kind)
    if binary is None:
        pytest.skip(f"{kind} CLI is not installed")
    rules = GameRules()
    protocol = MatchProtocol(end_condition="fixed_hands", hands=1)
    session = SeatSession("external", "External", rules, protocol, wait_timeout_seconds=10)
    stopped = Event()
    provider = OfflineProvider(kind, tmp_path)
    thread = Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    try:
        with ControllerServer([session]) as controller, ThreadPoolExecutor(max_workers=1) as worker:
            match = worker.submit(
                run_match,
                [HarnessBot(session), LowestFittingCardBot()],
                seed=123,
                match_id="installed-client-probe",
                seats=[PlayerSeat(player_id="external"), PlayerSeat(player_id="baseline")],
                rules=rules,
                protocol=protocol,
                config=RunConfig(decision_timeout_seconds=60),
                stop_event=stopped,
            )
            try:
                command, environment = _command(kind, binary, provider, session, controller.endpoint)
                process = subprocess.run(  # noqa: S603 - installed CLI with a fixed localhost-only fixture.
                    command,
                    input=b"Call play(proposal=null), then play the complete match.",
                    cwd=tmp_path,
                    env=environment,
                    capture_output=True,
                    timeout=90,
                    check=False,
                )
                (tmp_path / "stdout.jsonl").write_bytes(process.stdout)
                (tmp_path / "stderr.txt").write_bytes(process.stderr)
                assert provider.errors == [], provider.errors
                assert process.returncode == 0, process.stderr.decode()
                assert provider.terminal_seen, process.stdout.decode()
                result = match.result(timeout=5)
            finally:
                stopped.set()
                session.stop()
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=3)
    assert result.outcome == MatchOutcome.FINISHED
    assert result.actions_rejected == 0
    selection_sizes = []
    for schema, view in provider.decisions:
        card_fields = [node["properties"]["card"] for node in _objects(schema) if "card" in node.get("properties", {})]
        if kind == "codex":
            assert len(card_fields) > 0
            assert all(field["type"] == "integer" and "enum" not in field for field in card_fields)
        elif "select_card" in view.legal_actions:
            assert len(card_fields) == 1
            assert card_fields[0]["enum"] == list(view.you.hand)
            selection_sizes.append(len(view.you.hand))
        else:
            assert card_fields == []
    if kind == "claude":
        assert selection_sizes == list(range(10, 0, -1))
