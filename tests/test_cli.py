"""Tests for the command-line interface."""

import re
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

from sixnimmt.cli import app

runner = CliRunner()


@pytest.fixture
def players_file(tmp_path: Path) -> Path:
    path = tmp_path / "players.json"
    path.write_text('[{"bot": "random", "display_name": "Alice"}, {"bot": "random", "display_name": "Bob"}]')
    return path


def invoke_arena(players_file: Path, *arguments: str):
    return runner.invoke(
        app, ["arena", "--players-file", str(players_file), "--games", "1", "--seed", "1234", *arguments]
    )


def test_runs_arena_with_structured_players(players_file: Path) -> None:
    result = invoke_arena(players_file)
    assert result.exit_code == 0, result.output
    assert "Arena results" in result.stdout
    assert "Seed 1234" in result.stdout
    assert "1 requested · 1 started · 1 completed" in result.stdout
    assert re.search(r"Finished\s+Abandoned\s+Forfeited\s+Failed\s+[^\d]+1\s+0\s+0\s+0", result.stdout)
    assert re.search(r"Player\s+Bot\s+Wins\s+Ties\s+Total\s+Avg", result.stdout)
    assert re.search(r"Alice\s+random\s+\d+\s+\d+\s+\d+\s+\d+\.\d{2}", result.stdout)
    assert "Bob" in result.stdout
    assert "player_1" in result.stdout
    assert "player_2" in result.stdout
    assert "\x1b[" not in result.stdout


def test_same_arguments_produce_same_output(players_file: Path) -> None:
    assert invoke_arena(players_file).stdout == invoke_arena(players_file).stdout


def test_redirected_output_skips_the_animation(players_file: Path) -> None:
    result = invoke_arena(players_file)
    assert "The bull pen" not in result.stdout
    assert result.stdout == invoke_arena(players_file, "--no-animation").stdout


@pytest.mark.parametrize("backend, exit_code", [("thread", 0), ("process", 0), ("unknown", 2)])
def test_terminal_animation_restores_cursor_on_exit(players_file: Path, backend: str, exit_code: int) -> None:
    result = runner.invoke(
        app,
        ["arena", "--players-file", str(players_file), "--games", "1", "--backend", backend],
        env={"TTY_COMPATIBLE": "1", "TERM": "xterm", "COLUMNS": "80"},
    )
    assert result.exit_code == exit_code, result.output
    assert "The bull pen" in result.stdout
    assert "2 players at the doodle table" in result.stdout
    assert "Alice" in result.stdout
    assert "Bob" in result.stdout
    assert "Elapsed" in result.stdout
    assert "\x1b[?25l" in result.stdout
    assert "\x1b[?25h" in result.stdout
    if exit_code == 0:
        assert result.stdout.index("\x1b[?25h") < result.stdout.index("Arena results")


def test_animation_can_be_disabled_in_a_terminal(players_file: Path) -> None:
    result = runner.invoke(
        app,
        ["arena", "--players-file", str(players_file), "--games", "1", "--no-animation"],
        env={"TTY_COMPATIBLE": "1", "TERM": "xterm"},
    )
    assert result.exit_code == 0, result.output
    assert "The bull pen" not in result.stdout
    assert "\x1b[?25l" not in result.stdout


def test_process_backend_produces_same_cli_results(players_file: Path) -> None:
    sequential = invoke_arena(players_file, "--games", "4")
    parallel = invoke_arena(players_file, "--games", "4", "--backend", "process", "--concurrency", "2")
    assert parallel.exit_code == 0, parallel.output
    assert parallel.stdout == sequential.stdout


def test_cli_rejects_unknown_backend(players_file: Path) -> None:
    result = invoke_arena(players_file, "--backend", "unknown")
    assert result.exit_code == 2
    assert "unknown backend" in result.stderr


@pytest.mark.parametrize(
    "contents, message",
    [
        ('["random", "random"]', "Input should be an object"),
        ('[{"bot": "unknown"}, {"bot": "random"}]', "unknown bot"),
        ('[{"bot": "random", "options": {"typo": 1}}, {"bot": "random"}]', "invalid options for player 1"),
        ('[{"bot": "random", "display_nam": "Alice"}, {"bot": "random"}]', "Extra inputs"),
        ('[{"bot": "random"}]', "between 2 and 10"),
        ("not json", "Invalid JSON"),
    ],
)
def test_rejects_invalid_player_files(players_file: Path, contents: str, message: str) -> None:
    players_file.write_text(contents)
    result = invoke_arena(players_file)
    assert result.exit_code == 2
    assert message in result.stderr
    assert "Traceback" not in result.stderr


def test_reports_missing_player_file(tmp_path: Path) -> None:
    result = invoke_arena(tmp_path / "missing.json")
    assert result.exit_code == 2
    assert "cannot read players file" in result.stderr


@pytest.mark.parametrize("flag", ["--games", "--max-actions-per-match"])
def test_rejects_nonpositive_run_settings(players_file: Path, flag: str) -> None:
    result = invoke_arena(players_file, flag, "0")
    assert result.exit_code == 2
    assert "positive" in result.stderr


def test_reports_action_limit_abandonment(players_file: Path) -> None:
    result = invoke_arena(players_file, "--max-actions-per-match", "1")
    assert result.exit_code == 0
    assert re.search(r"Finished\s+Abandoned\s+Forfeited\s+Failed\s+[^\d]+0\s+1\s+0\s+0", result.stdout)
    assert "No finished matches; average scores are unavailable." in result.stdout
    assert re.search(r"Alice\s+random\s+0\s+0\s+0\s+—", result.stdout)


def test_cli_does_not_accept_name_only_players() -> None:
    result = runner.invoke(app, ["arena", "--players", "random", "random", "--games", "1", "--seed", "1234"])
    assert result.exit_code == 2
    error_text = Text.from_ansi(result.stderr).plain
    assert "No such option: --players" in error_text


@pytest.mark.arena_slow
def test_runs_multiple_arena_games(players_file: Path) -> None:
    result = invoke_arena(players_file, "--games", "10")
    assert result.exit_code == 0
    assert "10 requested · 10 started · 10 completed" in result.stdout


def test_communication_cli_traces_and_summarises(tmp_path: Path, players_file: Path) -> None:
    directory = tmp_path / "trace"
    result = invoke_arena(players_file, "--communication", "--concurrency", "2", "--trace-dir", str(directory))
    assert result.exit_code == 0, result.output
    assert re.search(r"Finished\s+Abandoned\s+Forfeited\s+Failed\s+[^\d]+1\s+0\s+0\s+0", result.stdout)
    logs = [path for path in directory.glob("*.jsonl") if not path.name.endswith(".actions.jsonl")]
    summary = runner.invoke(app, ["summarise", str(logs[0])])
    assert summary.exit_code == 0, summary.output
    assert '"has_manifest": true' in summary.stdout
    assert '"outcome": "finished"' in summary.stdout


def test_cli_refuses_starving_schedule(players_file: Path) -> None:
    result = invoke_arena(players_file, "--communication", "--scheduler", "sequential")
    assert result.exit_code == 2
    assert "starve" in result.stderr


def test_cli_warns_when_stopping_has_no_deadline(players_file: Path) -> None:
    result = invoke_arena(players_file, "--stop-on-failure")
    assert result.exit_code == 0
    assert "wait indefinitely" in result.stderr


def test_player_names_are_rendered_literally(players_file: Path) -> None:
    players_file.write_text('[{"bot": "random", "display_name": "[red]Alice[/red]"}, {"bot": "random"}]')
    result = invoke_arena(players_file)
    assert result.exit_code == 0, result.output
    assert "[red]Alice[/red]" in result.stdout


def test_arena_output_fits_a_narrow_terminal(players_file: Path) -> None:
    result = runner.invoke(
        app,
        ["arena", "--players-file", str(players_file), "--games", "1"],
        env={"COLUMNS": "60"},
    )
    assert result.exit_code == 0, result.output
    assert "Alice" in result.stdout
    assert "Bob" in result.stdout
    assert "Avg" in result.stdout
    assert all(len(line) <= 60 for line in result.stdout.splitlines())
