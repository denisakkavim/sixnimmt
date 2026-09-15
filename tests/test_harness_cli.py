"""Fixed watched tables preserve private setup and use the existing replay path."""

import json
import os
import signal
import stat
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from typer.testing import CliRunner

from sixnimmt.arena.bots.external_harnesses.connection import write_connection_bundle
from sixnimmt.arena.bots.external_harnesses.transport import SeatClient
from sixnimmt.arena.table import SetupTimeout, create_seats, wait_for_ready
from sixnimmt.cli import app
from sixnimmt.engine.rules import GameRules, MatchProtocol

runner = CliRunner()


def table_arguments(directory: Path, *seats: str) -> list[str]:
    arguments = ["table", "--output-dir", str(directory), "--hands", "1", "--retain-seconds", "0", "--quiet"]
    for seat in seats:
        arguments.extend(["--seat", seat])
    return arguments


def test_table_plays_exact_baseline_lineup_and_writes_replayable_traces(tmp_path: Path) -> None:
    directory = tmp_path / "table"
    result = runner.invoke(app, [*table_arguments(directory, "lowest_card", "highest_card"), "--auto-start"])
    assert result.exit_code == 0, result.output
    manifest = json.loads((directory / "traces" / "manifest.json").read_text())
    assert [seat["bot"] for seat in manifest["seats"]] == ["LowestCardBot", "HighestCardBot"]
    replay = runner.invoke(app, ["replay", str(directory / "traces" / "table.jsonl")])
    assert replay.exit_code == 0, replay.output
    assert "status=finished" in replay.output


def test_ready_table_requires_explicit_start_by_default(tmp_path: Path) -> None:
    directory = tmp_path / "table"
    result = runner.invoke(app, table_arguments(directory, "lowest_card", "highest_card"), input="n\n")
    assert result.exit_code == 2
    assert "All seats ready. Start the match?" in result.output
    assert not (directory / "traces").exists()


@pytest.mark.parametrize("seats", [("lowest_card",), ("unknown", "lowest_card"), ("command", "lowest_card")])
def test_invalid_lineup_is_rejected_before_creating_output(tmp_path: Path, seats: tuple[str, ...]) -> None:
    directory = tmp_path / "table"
    result = runner.invoke(app, [*table_arguments(directory, *seats), "--auto-start"])
    assert result.exit_code == 2
    assert not directory.exists()


@pytest.mark.parametrize("option", ["--setup-timeout", "--managed-timeout", "--wait-timeout", "--decision-timeout"])
def test_nonpositive_timeouts_are_rejected_before_creating_output(tmp_path: Path, option: str) -> None:
    directory = tmp_path / "table"
    result = runner.invoke(app, [*table_arguments(directory, "lowest_card", "highest_card"), option, "0"])
    assert result.exit_code == 2
    assert not directory.exists()


def test_native_setup_times_out_without_starting_the_game(tmp_path: Path) -> None:
    directory = tmp_path / "table"
    result = runner.invoke(
        app,
        [*table_arguments(directory, "codex", "claude"), "--auto-start", "--setup-timeout", "0.05"],
    )
    assert result.exit_code == 2, result.output
    assert "setup timed out" in result.output
    assert "open a separate terminal and run" in result.output
    assert (directory / "seats" / "player_1" / "launch.sh").is_file()
    assert not (directory / "traces").exists()


def test_connection_bundles_are_private_scoped_and_select_modern_mcp(tmp_path: Path) -> None:
    seats = create_seats(
        ["codex", "claude"],
        seed=123456789,
        directory=tmp_path,
        rules=GameRules(),
        protocol=MatchProtocol(),
    )
    for seat in seats:
        assert seat.session is not None
        assert seat.native_client is not None
        workspace = write_connection_bundle(seat.session, tmp_path, "tcp://127.0.0.1:12345", seat.native_client)
        prompt = (workspace / "prompt.txt").read_text()
        instructions = (workspace / "PLAY.md").read_text()
        assert seat.session.session_id in prompt
        assert seat.session.credential not in prompt + instructions
        assert "123456789" not in prompt + instructions
        assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
        assert stat.S_IMODE((workspace / "claude-mcp.json").stat().st_mode) == 0o600
        codex = tomllib.loads((workspace / "codex-settings.toml").read_text())
        assert codex["features"]["mcp_2026_07_28"] is True
        environment = codex["mcp_servers"]["sixnimmt"]["env"]
        assert environment["CODEX_MCP_PROTOCOL_VERSION"] == "2026-07-28"
        assert "SIXNIMMT_CREDENTIAL" not in environment
        assert codex["mcp_servers"]["sixnimmt"]["env_vars"] == ["SIXNIMMT_ENDPOINT", "SIXNIMMT_CREDENTIAL"]
        assert seat.session.credential not in (workspace / "codex-settings.toml").read_text()
        claude = json.loads((workspace / "claude-mcp.json").read_text())
        assert claude["mcpServers"]["sixnimmt"]["timeout"] == 660000
        syntax = subprocess.run(  # noqa: S603 - syntax-check a generated fixture without executing it.
            ["/bin/sh", "-n", str(workspace / "launch.sh")],
            capture_output=True,
            check=False,
        )
        assert syntax.returncode == 0
    claude_launch = (tmp_path / "seats" / "player_2" / "launch.sh").read_text()
    assert "exec env MCP_SDK_GENERATION=v2 MCP_PROTOCOL_NEGOTIATION=auto" in claude_launch


def test_readiness_requires_every_external_seat(tmp_path: Path) -> None:
    seats = create_seats(["codex", "claude"], seed=66, directory=tmp_path, rules=GameRules(), protocol=MatchProtocol())
    first = seats[0].session
    second = seats[1].session
    assert first is not None and second is not None
    first.ready.set()
    with pytest.raises(SetupTimeout, match="claude 2"):
        wait_for_ready(seats, 0.01, Event())
    second.ready.set()
    wait_for_ready(seats, 0.01, Event())


def test_external_notebook_is_explicit_and_configured_for_each_seat(tmp_path: Path) -> None:
    seats = create_seats(
        ["codex", "claude-headless"],
        seed=66,
        directory=tmp_path,
        rules=GameRules(),
        protocol=MatchProtocol(),
        memory_enabled=True,
        memory_max_chars=1234,
    )
    for seat in seats:
        assert seat.session is not None
        info = seat.session.get_game_info(seat.session.session_id)
        assert info["memory_enabled"] is True
        assert info["memory_max_chars"] == 1234


def test_native_launch_passes_credential_in_environment_without_command_arguments(tmp_path: Path) -> None:
    seats = create_seats(
        ["codex", "highest_card"], seed=66, directory=tmp_path, rules=GameRules(), protocol=MatchProtocol()
    )
    seat = seats[0]
    assert seat.session is not None
    assert seat.native_client is not None
    workspace = write_connection_bundle(seat.session, tmp_path, "tcp://127.0.0.1:12345", seat.native_client)
    binary = tmp_path / "codex"
    binary.write_text(
        f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps({"
        "'args':sys.argv[1:], 'credential':os.environ['SIXNIMMT_CREDENTIAL']}))\n",
    )
    binary.chmod(0o700)
    capture = tmp_path / "launch.json"
    result = subprocess.run(  # noqa: S603 - launch only the local fixture executable.
        [str(workspace / "launch.sh")],
        check=False,
        capture_output=True,
        env={**os.environ, "PATH": str(tmp_path), "CAPTURE": str(capture)},
    )
    assert result.returncode == 0
    recorded = json.loads(capture.read_text())
    assert recorded["credential"] == seat.session.credential
    assert seat.session.credential not in " ".join(recorded["args"])


def write_command_profile(tmp_path: Path, source: str, timeout: float = 2) -> Path:
    script = tmp_path / "agent.py"
    script.write_text(source, encoding="utf-8")
    profile = tmp_path / "command.json"
    profile.write_text(json.dumps({"command": [sys.executable, str(script)], "timeout_seconds": timeout}))
    return profile


@pytest.mark.parametrize("communication", [False, True])
def test_managed_command_completes_a_real_match_without_provider_calls(tmp_path: Path, communication: bool) -> None:
    source = (
        "import json, sys, uuid\n"
        "offer = json.load(sys.stdin)['offer']\n"
        "view = offer['view']\n"
        "if 'choose_row' in view['legal_actions']:\n"
        "    actions = [{'type':'choose_row', 'row_index':0}]\n"
        "else:\n"
        "    actions = [{'type':'select_card', 'card':min(view['you']['hand'])}]\n"
        "    if view['protocol']['communication_enabled']:\n"
        "        actions.append({'type':'commit'})\n"
        "result = {key:offer[key] for key in ('protocol_version','session_id','decision_id','view_id')}\n"
        "result.update(submission_id=str(uuid.uuid4()), actions=actions, memory=None)\n"
        "print(json.dumps(result))\n"
    )
    profile = write_command_profile(tmp_path, source)
    directory = tmp_path / "table"
    arguments = [*table_arguments(directory, f"command:{profile}", "highest_card"), "--auto-start"]
    if communication:
        arguments.append("--communication")
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.output
    summary = json.loads((directory / "result.json").read_text())
    assert summary["outcome"] == "finished"
    assert result.output.splitlines() == [
        f"Match finished. Winners: {', '.join(summary['winners'])}",
        f"Trace: {directory / 'traces' / 'table.jsonl'}",
    ]
    replay = runner.invoke(app, ["replay", str(directory / "traces" / "table.jsonl")])
    assert "status=finished" in replay.output


def test_managed_timeout_finishes_the_table_with_a_failure_trace(tmp_path: Path) -> None:
    profile = write_command_profile(tmp_path, "import time\ntime.sleep(10)\n", timeout=0.05)
    directory = tmp_path / "table"
    result = runner.invoke(
        app,
        [*table_arguments(directory, f"command:{profile}", "highest_card"), "--auto-start"],
    )
    assert result.exit_code == 1, result.output
    summary = json.loads((directory / "result.json").read_text())
    assert summary["outcome"] == "failed"
    assert summary["reason"] == "decision_timeout"


def test_managed_timeout_option_applies_when_command_profile_omits_it(tmp_path: Path) -> None:
    profile = write_command_profile(tmp_path, "import time\ntime.sleep(10)\n")
    settings = json.loads(profile.read_text())
    del settings["timeout_seconds"]
    profile.write_text(json.dumps(settings))
    directory = tmp_path / "table"
    result = runner.invoke(
        app,
        [
            *table_arguments(directory, f"command:{profile}", "highest_card"),
            "--auto-start",
            "--managed-timeout",
            "0.05",
        ],
    )
    assert result.exit_code == 1, result.output
    summary = json.loads((directory / "result.json").read_text())
    assert summary["reason"] == "decision_timeout"


@pytest.mark.parametrize("malformed", ["not JSON", '{"wrong": "schema"}'])
def test_managed_format_repair_keeps_the_same_offer_and_submits_once(tmp_path: Path, malformed: str) -> None:
    marker = tmp_path / "decision-id"
    source = (
        "import json, pathlib, sys, uuid\n"
        "request = json.load(sys.stdin)\n"
        "assert 'play(session_id)' not in request['game_info']['instructions']\n"
        "offer = request['offer']\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "if not marker.exists():\n"
        "    marker.write_text(offer['decision_id'])\n"
        f"    print({malformed!r})\n"
        "    sys.exit(0)\n"
        "assert marker.read_text() == offer['decision_id']\n"
        "assert 'protocol_error' in offer\n"
        "result = {key:offer[key] for key in ('protocol_version','session_id','decision_id','view_id')}\n"
        "result.update(submission_id=str(uuid.uuid4()), memory=None, "
        "actions=[{'type':'select_card', 'card':min(offer['view']['you']['hand'])}])\n"
        "print(json.dumps(result))\n"
    )
    profile = write_command_profile(tmp_path, source)
    directory = tmp_path / "table"
    result = runner.invoke(
        app,
        [
            *table_arguments(directory, f"command:{profile}", "highest_card"),
            "--auto-start",
            "--match-action-limit",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    actions = [json.loads(line) for line in (directory / "traces" / "table.actions.jsonl").read_text().splitlines()]
    assert len(actions) == 1
    assert actions[0]["outcome"] == "accepted"
    manifest = json.loads((directory / "traces" / "manifest.json").read_text())
    assert manifest["matches"][0]["seat_stats"]["player_1"]["invocations"] == 2


def test_managed_format_repair_does_not_reset_its_decision_budget(tmp_path: Path) -> None:
    profile = write_command_profile(tmp_path, "import time\ntime.sleep(0.18)\nprint('bad final JSON')\n", timeout=0.25)
    directory = tmp_path / "table"
    result = runner.invoke(app, [*table_arguments(directory, f"command:{profile}", "highest_card"), "--auto-start"])
    assert result.exit_code == 1, result.output
    summary = json.loads((directory / "result.json").read_text())
    assert summary["reason"] == "decision_timeout"


def test_managed_format_repair_stops_after_one_unsuccessful_correction(tmp_path: Path) -> None:
    profile = write_command_profile(tmp_path, "print('bad final JSON')\n")
    directory = tmp_path / "table"
    result = runner.invoke(app, [*table_arguments(directory, f"command:{profile}", "highest_card"), "--auto-start"])
    assert result.exit_code == 1, result.output
    manifest = json.loads((directory / "traces" / "manifest.json").read_text())
    assert manifest["matches"][0]["seat_stats"]["player_1"]["invocations"] == 2


def test_managed_only_table_does_not_retain_results_without_a_listener(tmp_path: Path) -> None:
    profile = write_command_profile(tmp_path, "print('bad final JSON')\n")
    result = runner.invoke(
        app,
        [
            *table_arguments(tmp_path / "table", f"command:{profile}", "highest_card"),
            "--auto-start",
            "--retain-seconds",
            "1",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "Keeping seat results available" not in result.output


def _read_seat_client(directory: Path, player_id: str) -> tuple[SeatClient, str]:
    workspace = directory / "seats" / player_id
    deadline = time.monotonic() + 5
    while not (workspace / "launch.sh").exists():
        if time.monotonic() >= deadline:
            pytest.fail("native seat bundle was not created")
        time.sleep(0.01)
    config = json.loads((workspace / "claude-mcp.json").read_text())
    environment = config["mcpServers"]["sixnimmt"]["env"]
    client = SeatClient(environment["SIXNIMMT_ENDPOINT"], environment["SIXNIMMT_CREDENTIAL"])
    session_id = json.loads((workspace / "game-info.json").read_text())["session_id"]
    return client, session_id


@pytest.mark.skipif(os.name != "posix", reason="exercise the terminal SIGINT path")
def test_operator_stop_preserves_pending_terminal_reply_and_read_only_recovery(tmp_path: Path) -> None:
    directory = tmp_path / "table"
    command = [
        sys.executable,
        "-c",
        "from sixnimmt.cli import app; app()",
        *table_arguments(directory, "codex", "claude"),
        "--auto-start",
        "--retain-seconds",
        "1",
    ]
    process = subprocess.Popen(  # noqa: S603 - fixed local CLI entry point, no provider is launched.
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    cancel = Event()
    try:
        first, first_id = _read_seat_client(directory, "player_1")
        second, second_id = _read_seat_client(directory, "player_2")
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_offer = executor.submit(first.play, first_id, cancel=cancel)
            waiting = executor.submit(second.play, second_id, cancel=cancel)
            try:
                assert first_offer.result(timeout=5)["status"] == "decision"
                process.send_signal(signal.SIGINT)
                terminal = waiting.result(timeout=5)
                assert terminal["status"] == "terminal"
                assert terminal["result"]["reason"] == "operator_stop"
                assert process.poll() is None
                recovered = first.play(first_id, cancel=cancel)
                assert recovered["status"] == "terminal"
                assert recovered["result"]["outcome"] == "abandoned"
            finally:
                cancel.set()
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 130, (stdout + stderr).decode()
        assert b"Keeping seat results available" in stdout
    finally:
        cancel.set()
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)
