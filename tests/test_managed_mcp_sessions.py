"""Managed vendor sessions use the real MCP bridge for an entire offline match."""

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sixnimmt.arena.artifacts import load_run
from sixnimmt.arena.bots.external_harnesses.drivers import CommandProfile, DriverError, ManagedCommandDriver
from sixnimmt.arena.sessions import resolve_external_seat
from sixnimmt.cli import app

FAKE_HARNESS = Path(__file__).parent / "fixtures" / "fake_mcp_harness.py"


def run_vendor(tmp_path: Path, kind: str, *, source: str | None = None, timeout: float = 5) -> tuple[Any, Path]:
    script = FAKE_HARNESS
    if source is not None:
        script = tmp_path / "harness.py"
        script.write_text(source)
    config = {
        "catalogue": [
            {
                "key": "agent",
                "bot": kind + "-headless",
                "options": {
                    "command": [sys.executable, str(script)],
                    "model": "configured-model",
                    "reasoning_effort": "high",
                    "timeout_seconds": timeout,
                    "max_output_bytes": 500,
                },
            },
            {"bot": "lowest_card"},
        ],
        "lineup": ["agent", "lowest_card"],
        "protocol": {"end_condition": "fixed_hands", "hands": 1},
        "session": {"auto_start": True, "setup_timeout": 5, "retain_seconds": 0},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    directory = tmp_path / "run"
    result = CliRunner().invoke(app, ["play", "--config", str(config_path), "--output-dir", str(directory), "--trace"])
    return result, directory


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_vendor_uses_one_mcp_process_for_a_complete_match_and_receives_terminal(tmp_path: Path, kind: str) -> None:
    result, directory = run_vendor(tmp_path, kind)
    assert result.exit_code == 0, result.output
    record = load_run(directory).results[0]
    stats = record.seat_stats[0]
    assert stats is not None
    assert stats["invocations"] == 1
    assert stats["context"] == "match"
    assert record.actions_accepted >= 20
    assert record.actions_rejected == 0
    workspaces = list(directory.glob("matches/*/seats/player_1"))
    assert len(workspaces) == 1
    receipt = json.loads((workspaces[0] / "terminal-received.json").read_text())
    assert receipt["count"] == record.seat_actions[0][0]
    schemas = json.loads((workspaces[0] / "observed-schemas.json").read_text())
    unique_schemas = {json.dumps(schema, sort_keys=True) for schema in schemas}
    assert len(unique_schemas) > 2 if kind == "claude" else len(unique_schemas) == 1


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_vendor_exit_during_setup_fails_without_waiting_for_setup_timeout(tmp_path: Path, kind: str) -> None:
    started = time.monotonic()
    source = "import sys\nprint('fixture startup diagnostic', file=sys.stderr)\nraise SystemExit(3)\n"
    result, directory = run_vendor(tmp_path, kind, source=source)
    assert result.exit_code == 1, result.output
    assert "status 3" in result.output
    assert time.monotonic() - started < 3
    assert load_run(directory).results == ()
    traces = list(directory.rglob("*.model.jsonl"))
    assert len(traces) == 1
    events = [json.loads(line) for line in traces[0].read_text().splitlines()]
    failures = [event for event in events if event["type"] == "invocation_failed"]
    assert len(failures) == 1
    assert failures[0]["stderr"] == "fixture startup diagnostic\n"


def test_vendor_final_json_without_mcp_does_not_become_a_move(tmp_path: Path) -> None:
    result, directory = run_vendor(tmp_path, "codex", source="print('{\"actions\":[]}')\n")
    assert result.exit_code == 1, result.output
    assert "exited before the match" in result.output
    assert load_run(directory).results == ()


def test_custom_command_cannot_accidentally_use_match_mcp_lifecycle(tmp_path: Path) -> None:
    driver = ManagedCommandDriver(CommandProfile((sys.executable, "-c", "print('{}')")), tmp_path)
    with pytest.raises(DriverError, match="fresh JSON invocation contract"):
        driver.run_match(tmp_path, {}, dict, wait_timeout_seconds=60)


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_managed_mcp_decision_timeout_reaps_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setenv("SIXNIMMT_TEST_HANG_AT_DECISION", "1")
    started = time.monotonic()
    result, directory = run_vendor(tmp_path, kind, timeout=0.1)
    assert result.exit_code == 1, result.output
    assert time.monotonic() - started < 5
    record = load_run(directory).results[0]
    assert record.reason == "decision_timeout"
    assert record.actions_accepted == 0
    traces = list(directory.rglob("*.model.jsonl"))
    events = [json.loads(line) for line in traces[0].read_text().splitlines()]
    assert len([event for event in events if event["type"] == "invocation_started"]) == 1


@pytest.mark.parametrize(
    "strategy, expected",
    [("codex", "static"), ("codex-headless", "static"), ("claude", "dynamic"), ("claude-headless", "dynamic")],
)
def test_external_metadata_records_the_actual_tool_schema_policy(strategy: str, expected: str) -> None:
    resolved = resolve_external_seat(strategy, {})
    assert resolved.metadata["tool_schema_mode"] == expected
