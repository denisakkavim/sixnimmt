"""Offline Codex/Claude stand-in that drives the real game MCP bridge."""

import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class Harness:
    def __init__(self) -> None:
        self.request_id = 0
        self.results: dict[int, dict[str, Any]] = {}
        self.schemas: list[dict[str, Any]] = []
        self.process = subprocess.Popen(
            [sys.executable, "-m", "sixnimmt.arena.bots.external_harnesses.mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
        )

    def send(self, method: str, params: dict[str, Any]) -> int:
        self.request_id += 1
        params = {
            **params,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        }
        message = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()
        return self.request_id

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.send(method, params)
        while request_id not in self.results:
            assert self.process.stdout is not None
            line = self.process.stdout.readline()
            assert line != b"", "MCP bridge exited before replying"
            response = json.loads(line)
            if response.get("method") == "notifications/tools/list_changed":
                self.send("tools/list", {})
                continue
            assert "error" not in response, response
            if isinstance(response.get("id"), int):
                self.results[response["id"]] = response["result"]
        return self.results.pop(request_id)

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        assert response.get("isError") is not True, response
        return response["structuredContent"]

    def record_tools(self) -> dict[str, Any]:
        result = self.request("tools/list", {})
        tools = {tool["name"]: tool["inputSchema"] for tool in result["tools"]}
        assert tools["get_game_info"]["properties"] == {}
        self.schemas.append(tools["play"])
        return tools["play"]

    def close(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()
        self.process.wait(timeout=3)
        assert self.process.stdout is not None
        self.process.stdout.close()


def validate_launch() -> str:
    arguments = sys.argv[1:]
    assert "--output-schema" not in arguments
    assert "--output-last-message" not in arguments
    assert "--json-schema" not in arguments
    if "--model" in arguments:
        assert arguments[arguments.index("--model") + 1] == "configured-model"
    if "exec" in arguments:
        settings: dict[str, Any] = {}
        for index, argument in enumerate(arguments[:-1]):
            if argument == "-c":
                settings.update(tomllib.loads(arguments[index + 1]))
        if "model_reasoning_effort" in settings:
            assert settings["model_reasoning_effort"] == "high"
        bridge = settings["mcp_servers"]["sixnimmt"]
        assert bridge["env"]["CODEX_MCP_PROTOCOL_VERSION"] == "2026-07-28"
        assert "SIXNIMMT_SESSION_ID" in bridge["env_vars"]
        assert bridge["tools"] == {
            "get_game_info": {"approval_mode": "approve"},
            "play": {"approval_mode": "approve"},
        }
        return "codex"
    if "--effort" in arguments:
        assert arguments[arguments.index("--effort") + 1] == "high"
        assert os.environ["CLAUDE_CODE_EFFORT_LEVEL"] == "high"
    assert os.environ["MCP_SDK_GENERATION"] == "v2"
    assert os.environ["CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS"] == "0"
    bridge = json.loads(Path(arguments[arguments.index("--mcp-config") + 1]).read_text())["mcpServers"]["sixnimmt"]
    assert bridge["env"]["SIXNIMMT_SESSION_ID"] == os.environ["SIXNIMMT_SESSION_ID"]
    assert "mcp__sixnimmt__play" in arguments
    return "claude"


def comment(kind: str, count: int) -> None:
    text = f"Decision {count}: choose the lowest legal card."
    if kind == "codex":
        record = {"type": "item.completed", "item": {"id": str(count), "type": "agent_message", "text": text}}
    else:
        record = {"type": "assistant", "message": {"id": str(count), "content": [{"type": "text", "text": text}]}}
    print(json.dumps(record), flush=True)


def card_choices(node: object) -> list[list[int]]:
    if isinstance(node, list):
        return [choices for child in node for choices in card_choices(child)]
    if not isinstance(node, dict):
        return []
    properties = node.get("properties", {})
    if isinstance(properties, dict) and "card" in properties:
        return [properties["card"]["enum"]]
    return [choices for child in node.values() for choices in card_choices(child)]


def main() -> None:
    kind = validate_launch()
    prompt = sys.stdin.read()
    assert "PLAY.md" in prompt
    harness = Harness()
    count = 0
    started = time.monotonic()
    try:
        harness.send("subscriptions/listen", {"notifications": {"toolsListChanged": True}})
        harness.record_tools()
        info = harness.call("get_game_info", {})
        assert info["session_id"] == os.environ["SIXNIMMT_SESSION_ID"]
        reply = harness.call("play", {"proposal": None})
        while reply["status"] != "terminal":
            if reply["status"] == "wait_expired":
                reply = harness.call("play", {"proposal": None})
                continue
            assert reply["status"] == "decision", reply
            offer = reply["offer"]
            schema = harness.record_tools()
            count += 1
            if os.environ.get("SIXNIMMT_TEST_HANG_AT_DECISION") == "1":
                time.sleep(10)
            view = offer["view"]
            action = {"type": "select_card", "card": min(view["you"]["hand"])}
            if "choose_row" in view["legal_actions"]:
                action = {"type": "choose_row", "row_index": 0}
                if os.environ["SIXNIMMT_SCHEMA_MODE"] == "dynamic":
                    assert card_choices(schema) == []
            elif os.environ["SIXNIMMT_SCHEMA_MODE"] == "dynamic":
                assert card_choices(schema) == [view["you"]["hand"]]
            proposal = {
                "decision_id": offer["decision_id"],
                "submission_id": f"move-{count}",
                "actions": [action],
                "memory": None,
                "explanation": "I choose the lowest available card or first row.",
            }
            Draft202012Validator(schema).validate({"proposal": proposal})
            comment(kind, count)
            reply = harness.call("play", {"proposal": proposal})
        Path("terminal-received.json").write_text(json.dumps({"count": count, "elapsed": time.monotonic() - started}))
        Path("observed-schemas.json").write_text(json.dumps(harness.schemas))
        # Final JSON-looking text is not a game submission.
        print('{"actions":[{"type":"select_card","card":104}]}', flush=True)
    finally:
        harness.close()


if __name__ == "__main__":
    main()
