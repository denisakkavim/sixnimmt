"""Tests for the command-line interface: arenas, and replaying a match log."""

from pathlib import Path

import pytest
from conftest import ADMIN_TOKEN, open_match, play_to_completion
from starlette.testclient import TestClient
from typer.testing import CliRunner

from sixnimmt_server.cli import app
from sixnimmt_server.persistence.sink import event_log_path
from sixnimmt_server.server.app import create_app

runner = CliRunner()


def invoke_arena(*arguments: str):
    return runner.invoke(app, ["arena", *arguments])


def assert_successful_arena(output: str) -> None:
    assert "seed=1234" in output
    assert "games=1" in output
    assert "total_hands=" in output
    assert "total_actions=" in output
    assert "player_1 bot=random" in output
    assert "player_2 bot=random" in output
    assert "average_score=" in output


def test_runs_arena_with_space_separated_players() -> None:
    result = invoke_arena("--players", "random", "random", "--games", "1", "--seed", "1234")

    assert result.exit_code == 0
    assert_successful_arena(result.stdout)


def test_runs_arena_with_repeated_players() -> None:
    result = invoke_arena(
        "--players",
        "random",
        "--players",
        "random",
        "--games",
        "1",
        "--seed",
        "1234",
    )

    assert result.exit_code == 0
    assert_successful_arena(result.stdout)


def test_runs_arena_with_equals_players() -> None:
    result = invoke_arena("--players=random", "--players=random", "--games=1", "--seed=1234")

    assert result.exit_code == 0
    assert_successful_arena(result.stdout)


def test_same_arguments_produce_same_output() -> None:
    arguments = ("--players", "random", "random", "--games", "1", "--seed", "1234")

    first = invoke_arena(*arguments)
    second = invoke_arena(*arguments)

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert first.stdout == second.stdout


def test_reports_runner_validation_errors_without_traceback() -> None:
    result = invoke_arena("--players", "random", "unknown", "--games", "1", "--seed", "1234")

    assert result.exit_code == 2
    assert "unknown bot 'unknown'; available bots: greedy, random" in result.stderr
    assert "Traceback" not in result.stderr


def test_rejects_too_few_players() -> None:
    result = invoke_arena("--players", "random", "--games", "1", "--seed", "1234")

    assert result.exit_code == 2
    assert "an arena match requires between 2 and 10 players" in result.stderr


def test_rejects_non_positive_games() -> None:
    result = invoke_arena("--players", "random", "random", "--games", "0", "--seed", "1234")

    assert result.exit_code == 2
    assert "games must be positive" in result.stderr


def test_rejects_non_positive_action_limit() -> None:
    result = invoke_arena(
        "--players",
        "random",
        "random",
        "--games",
        "1",
        "--seed",
        "1234",
        "--max-actions-per-match",
        "0",
    )

    assert result.exit_code == 2
    assert "match_action_limit must be a positive integer" in result.stderr


def test_reports_action_limit_exhaustion_without_traceback() -> None:
    result = invoke_arena(
        "--players",
        "random",
        "random",
        "--games",
        "1",
        "--seed",
        "1234",
        "--max-actions-per-match",
        "1",
    )

    assert result.exit_code == 0
    assert "abandoned=1" in result.stdout
    assert "Traceback" not in result.stderr


def test_rejects_missing_player_value() -> None:
    result = invoke_arena("--players", "--games", "1", "--seed", "1234")

    assert result.exit_code == 2
    assert "Option '--players' requires an argument" in result.stderr


def test_rejects_unknown_options() -> None:
    result = invoke_arena(
        "--players",
        "random",
        "random",
        "--games",
        "1",
        "--seed",
        "1234",
        "--bogus",
    )

    assert result.exit_code == 2
    assert "No such option: --bogus" in result.stderr


def test_rejects_unexpected_argument_after_equals_players() -> None:
    result = invoke_arena("--players=random", "random", "--games", "1", "--seed", "1234")

    assert result.exit_code == 2
    assert "Got unexpected extra argument" in result.stderr
    assert "random" in result.stderr


@pytest.mark.arena_slow
def test_runs_multiple_arena_games() -> None:
    result = invoke_arena("--players", "random", "random", "--games", "10", "--seed", "1234")

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


def test_negotiation_cli_traces_and_summarises(tmp_path: Path) -> None:
    directory = tmp_path / "trace"
    result = invoke_arena(
        "--players",
        "random",
        "greedy",
        "--games",
        "1",
        "--seed",
        "123",
        "--negotiation",
        "--concurrency",
        "2",
        "--trace-dir",
        str(directory),
    )
    assert result.exit_code == 0, result.output
    assert "finished=1 abandoned=0 forfeited=0 failed=0" in result.stdout
    logs = [path for path in directory.glob("*.jsonl") if not path.name.endswith(".actions.jsonl")]
    summary = runner.invoke(app, ["summarise", str(logs[0])])
    assert summary.exit_code == 0, summary.output
    assert '"has_manifest": true' in summary.stdout
    assert '"outcome": "finished"' in summary.stdout


def test_cli_refuses_starving_schedule() -> None:
    result = invoke_arena(
        "--players", "random", "random", "--games", "1", "--seed", "123", "--negotiation", "--scheduler", "sequential"
    )
    assert result.exit_code == 2
    assert "starve" in result.stderr


def test_cli_warns_when_stopping_has_no_deadline() -> None:
    result = invoke_arena("--players", "random", "random", "--games", "1", "--seed", "123", "--stop-on-failure")
    assert result.exit_code == 0
    assert "wait indefinitely" in result.stderr
