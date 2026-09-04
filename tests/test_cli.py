"""Tests for the deterministic arena command-line interface."""

import pytest
from typer.testing import CliRunner

from sixnimmt_server.cli import app

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
    assert "unknown bot 'unknown'; available bots: random" in result.stderr
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
    assert "max_actions_per_match must be positive" in result.stderr


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

    assert result.exit_code == 1
    assert "action limit 1 exhausted" in result.stderr
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
