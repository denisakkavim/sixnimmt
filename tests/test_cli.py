"""Tests for the command-line interface: arenas, and replaying a match log."""

from pathlib import Path

import pytest
from click import unstyle
from conftest import ADMIN_TOKEN, open_match, play_to_completion
from starlette.testclient import TestClient
from typer.testing import CliRunner

from sixnimmt_server.cli import app
from sixnimmt_server.persistence.sink import event_log_path
from sixnimmt_server.server.app import create_app

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
    assert "player_1 bot=random" in result.stdout
    assert "player_2 bot=random" in result.stdout
    assert "finished=1" in result.stdout


def test_same_arguments_produce_same_output(players_file: Path) -> None:
    assert invoke_arena(players_file).stdout == invoke_arena(players_file).stdout


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
    assert "abandoned=1" in result.stdout


def test_cli_does_not_accept_name_only_players() -> None:
    result = runner.invoke(app, ["arena", "--players", "random", "random", "--games", "1", "--seed", "1234"])
    assert result.exit_code == 2
    assert "No such option: --players" in unstyle(result.stderr)


@pytest.mark.arena_slow
def test_runs_multiple_arena_games(players_file: Path) -> None:
    result = invoke_arena(players_file, "--games", "10")
    assert result.exit_code == 0
    assert "games=10" in result.stdout


def _write_match_log(directory: Path) -> tuple[Path, dict]:
    """Play a full match through the server and return its log and final state."""
    app = create_app(admin_token=ADMIN_TOKEN, log_directory=directory)
    with TestClient(app) as client:
        match = open_match(client)
        match.start()
        play_to_completion(match)
        return event_log_path(directory, match.match_id), match.state("omniscient")


def test_replay_prints_the_final_state_of_a_match_log(tmp_path: Path) -> None:
    path, final = _write_match_log(tmp_path)

    result = runner.invoke(app, ["replay", str(path)])

    assert result.exit_code == 0
    assert "status=finished" in result.stdout
    assert f"hands={final['hand_number']}" in result.stdout
    for player in final["players"]:
        assert f"{player['player_id']} total_score={player['total_score']}" in result.stdout


def test_replay_names_the_winners_the_log_recorded(tmp_path: Path) -> None:
    path, final = _write_match_log(tmp_path)
    scores = {player["player_id"]: player["total_score"] for player in final["players"]}
    lowest = min(scores.values())

    result = runner.invoke(app, ["replay", str(path)])

    winners = sorted(player_id for player_id, score in scores.items() if score == lowest)
    assert f"winners={','.join(winners)}" in result.stdout


def test_replay_refuses_a_path_that_is_not_a_match_log(tmp_path: Path) -> None:
    not_a_log = tmp_path / "notes.txt"
    not_a_log.write_text("this is not a match log\n")

    result = runner.invoke(app, ["replay", str(not_a_log)])

    assert result.exit_code == 2
    assert "not a match log" in result.stderr


def test_replay_reports_a_missing_log(tmp_path: Path) -> None:
    result = runner.invoke(app, ["replay", str(tmp_path / "absent.jsonl")])

    assert result.exit_code == 2
    assert "no match log" in result.stderr


def test_negotiation_cli_traces_and_summarises(tmp_path: Path, players_file: Path) -> None:
    directory = tmp_path / "trace"
    result = invoke_arena(players_file, "--negotiation", "--concurrency", "2", "--trace-dir", str(directory))
    assert result.exit_code == 0, result.output
    assert "finished=1 abandoned=0 forfeited=0 failed=0" in result.stdout
    logs = [path for path in directory.glob("*.jsonl") if not path.name.endswith(".actions.jsonl")]
    summary = runner.invoke(app, ["summarise", str(logs[0])])
    assert summary.exit_code == 0, summary.output
    assert '"has_manifest": true' in summary.stdout
    assert '"outcome": "finished"' in summary.stdout


def test_cli_refuses_starving_schedule(players_file: Path) -> None:
    result = invoke_arena(players_file, "--negotiation", "--scheduler", "sequential")
    assert result.exit_code == 2
    assert "starve" in result.stderr


def test_cli_warns_when_stopping_has_no_deadline(players_file: Path) -> None:
    result = invoke_arena(players_file, "--stop-on-failure")
    assert result.exit_code == 0
    assert "wait indefinitely" in result.stderr
