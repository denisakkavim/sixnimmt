"""Watched tables share the arena catalogue vocabulary and option precedence."""

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sixnimmt.arena.artifacts import load_run
from sixnimmt.arena.bots.external import ManagedHarnessBot
from sixnimmt.arena.bots.llm import LLMBot
from sixnimmt.arena.config import SessionOptions
from sixnimmt.arena.planning import RunSettings
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.arena.sessions import SeatConstructionError, prepare_seats
from sixnimmt.cli import app
from sixnimmt.engine.rules import GameRules, MatchProtocol

runner = CliRunner()


@pytest.fixture
def table_settings() -> dict[str, Any]:
    return {
        "catalogue": [
            {
                "key": "careful",
                "label": "Conservative",
                "family": "captures_and_bait",
                "bot": "controlled_burn",
                "options": {"K": 3, "fallback_strategy": "closest_gap"},
            },
            {"bot": "highest_card"},
        ],
        "lineup": ["careful", "highest_card"],
        "seed": 731,
        "rules": {"target_score": 99},
        "protocol": {"end_condition": "fixed_hands", "hands": 1, "communication_enabled": True},
        "execution": {
            "scheduler": "round_robin",
            "decision_timeout_seconds": 5,
            "match_action_limit": 400,
            "play_action_limit": 45,
            "decision_rejection_limit": 6,
            "max_abandoned_decisions": 0,
        },
    }


@pytest.fixture
def config_file(tmp_path: Path, table_settings: dict[str, Any]) -> Path:
    path = tmp_path / "table.json"
    path.write_text(json.dumps(table_settings), encoding="utf-8")
    return path


def table_arguments(config_file: Path, directory: Path) -> list[str]:
    return [
        "play",
        "--watch",
        "--trace",
        "--config",
        str(config_file),
        "--output-dir",
        str(directory),
        "--auto-start",
        "--quiet",
        "--retain-seconds",
        "0",
    ]


def test_catalogue_keys_labels_and_options_resolve_in_lineup_order(table_settings: dict[str, Any]) -> None:
    table_settings["lineup"] = ["highest_card", "careful"]
    players = RunSettings.model_validate(table_settings).players()
    assert [player.bot for player in players] == ["highest_card", "controlled_burn"]
    assert [player.display_name for player in players] == ["highest_card", "Conservative"]
    assert players[1].options == {"K": 3, "fallback_strategy": "closest_gap"}


@pytest.mark.parametrize(
    "updates",
    [
        {"lineup": ["careful"]},
        {"lineup": ["careful"] * 11},
        {"lineup": ["careful", "missing"]},
        {"rules": {"min_players": 3}},
        {"catalogue": [{"bot": "lowest_card", "key": "same"}, {"bot": "highest_card", "key": "same"}]},
        {"catalogue": [{"bot": "lowest_card"}, {"bot": "lowest_card"}]},
        {"catalogue": [{"bot": "lowest_card", "key": ""}]},
        {"catalogue": [{"bot": "lowest_card", "label": ""}]},
        {"catalogue": [{"bot": "lowest_card", "display_name": "Alice"}]},
        {"players": []},
        {"execution": {"typo": 1}},
    ],
    ids=[
        "one-seat",
        "eleven-seats",
        "unknown-key",
        "rules-player-count",
        "duplicate-explicit-key",
        "duplicate-default-key",
        "empty-key",
        "empty-label",
        "unsupported-candidate-field",
        "unsupported-top-level-field",
        "unsupported-execution-field",
    ],
)
def test_invalid_table_configuration_is_rejected(table_settings: dict[str, Any], updates: dict[str, Any]) -> None:
    table_settings.update(updates)
    with pytest.raises(ValueError):
        RunSettings.model_validate(table_settings).players()


def test_configuration_requires_a_lineup_before_constructing_players() -> None:
    settings = RunSettings.model_validate({"catalogue": [{"bot": "lowest_card"}]})
    with pytest.raises(ValueError):
        settings.players()


@pytest.mark.parametrize("backend", ["thread", "process"])
def test_fixed_configuration_allows_parallel_execution(table_settings: dict[str, Any], backend: str) -> None:
    table_settings["execution"]["backend"] = backend
    table_settings["execution"]["concurrency"] = 2
    settings = RunSettings.model_validate(table_settings)
    assert settings.execution.backend == backend
    assert settings.execution.concurrency == 2
    assert settings.games == 1
    assert settings.player_counts == (2,)


def test_fixed_lineup_repeats_requested_games_with_parallel_workers(config_file: Path, tmp_path: Path) -> None:
    directory = tmp_path / "run"
    result = runner.invoke(
        app,
        [
            "play",
            "--config",
            str(config_file),
            "--output-dir",
            str(directory),
            "--games",
            "3",
            "--concurrency",
            "2",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    run = load_run(directory)
    assert len(run.results) == 3
    assert run.plan.execution.concurrency == 2
    catalogue = {entry.config_id: entry for entry in run.plan.catalogue}
    assert all(
        [catalogue[seat.config_id].bot for seat in record.seats] == ["controlled_burn", "highest_card"]
        for record in run.results
    )
    assert len({job.match_seed for job in run.plan.jobs}) == 3
    assert not (directory / "traces").exists()
    assert json.loads(result.stdout)["report"]["diagnostics"]["finished_matches"] == 3


def test_configuration_without_lineup_can_select_catalogue_keys_from_cli(tmp_path: Path) -> None:
    config_file = tmp_path / "run.json"
    config_file.write_text(
        json.dumps({
            "catalogue": [{"key": "small", "bot": "lowest_card"}],
            "seed": 731,
            "protocol": {"end_condition": "fixed_hands", "hands": 1},
        })
    )
    directory = tmp_path / "run"
    result = runner.invoke(app, [*table_arguments(config_file, directory), "--seat", "small", "--seat", "highest_card"])
    assert result.exit_code == 0, result.output
    run = load_run(directory)
    assert len(run.results) == 1
    catalogue = {entry.config_id: entry for entry in run.plan.catalogue}
    assert [catalogue[seat.config_id].bot for seat in run.results[0].seats] == ["lowest_card", "highest_card"]


def test_custom_command_profile_can_be_defined_inside_catalogue(tmp_path: Path) -> None:
    settings = RunSettings.model_validate({
        "catalogue": [
            {
                "key": "agent",
                "bot": "command",
                "options": {"command": ["example-agent", "--json"], "timeout_seconds": 15, "max_output_bytes": 4096},
            },
            {"bot": "lowest_card"},
        ],
        "lineup": ["agent", "lowest_card"],
    })
    seats = prepare_seats(
        settings.players(),
        seed=66,
        directory=tmp_path,
        rules=settings.rules,
        protocol=settings.protocol,
        session_options=settings.session,
    )
    driver = seats[0].driver
    assert driver is not None
    assert driver.profile.command == ("example-agent", "--json")
    assert driver.profile.timeout_seconds == 15
    assert driver.profile.max_output_bytes == 4096


def test_llm_and_headless_models_use_the_same_catalogue_options_field(tmp_path: Path) -> None:
    settings = RunSettings.model_validate({
        "catalogue": [
            {"key": "codex", "bot": "codex-headless", "options": {"model": "codex-example"}},
            {"key": "claude", "bot": "claude-headless", "options": {"model": "claude-example"}},
            {
                "key": "api",
                "bot": "llm",
                "options": {"model": "api-example", "base_url": "http://127.0.0.1:1/v1"},
            },
        ],
        "lineup": ["codex", "claude", "api"],
    })
    seats = prepare_seats(
        settings.players(),
        seed=66,
        directory=tmp_path,
        rules=settings.rules,
        protocol=settings.protocol,
        session_options=settings.session,
    )
    for seat, model in zip(seats[:2], ("codex-example", "claude-example"), strict=True):
        assert isinstance(seat.bot, ManagedHarnessBot)
        assert seat.driver is not None
        assert seat.driver.profile.model == model
        recorded = seat.seat.agent_metadata["bot_options"]
        assert isinstance(recorded, dict)
        assert recorded["model"] == model
        assert seat.worker is not None
        assert seat.worker.thread.ident is None
    assert isinstance(seats[2].bot, LLMBot)
    assert seats[2].bot.options.model == "api-example"
    api_options = seats[2].seat.agent_metadata["bot_options"]
    assert isinstance(api_options, dict)
    assert api_options["model"] == "api-example"


def test_repeated_catalogue_key_constructs_independent_headless_sessions(tmp_path: Path) -> None:
    settings = RunSettings.model_validate({
        "catalogue": [{"key": "agent", "bot": "codex-headless", "options": {"model": "example"}}],
        "lineup": ["agent", "agent"],
    })
    first, second = prepare_seats(
        settings.players(),
        seed=66,
        directory=tmp_path,
        rules=settings.rules,
        protocol=settings.protocol,
        session_options=settings.session,
    )
    assert first.bot is not second.bot
    assert first.session is not None
    assert second.session is not None
    assert first.session.session_id != second.session.session_id
    assert first.session.credential != second.session.credential
    assert first.driver is not None
    assert second.driver is not None
    assert first.driver.workspace != second.driver.workspace
    assert first.driver.profile.model == second.driver.profile.model == "example"


@pytest.mark.parametrize("bot", ["codex", "claude"])
@pytest.mark.parametrize("option", ["model", "reasoning_effort"])
def test_native_seats_reject_model_and_reasoning_options(tmp_path: Path, bot: str, option: str) -> None:
    with pytest.raises(SeatConstructionError, match="native") as error:
        prepare_seats(
            [PlayerConfig(bot=bot, options={option: "example"}), PlayerConfig(bot="lowest_card")],
            seed=66,
            directory=tmp_path,
            rules=GameRules(),
            protocol=MatchProtocol(),
            session_options=SessionOptions(),
        )
    assert error.value.seat == 0
    assert isinstance(error.value.cause, ValueError)


@pytest.mark.parametrize("bot", ["codex-headless", "claude-headless"])
@pytest.mark.parametrize("options", [{"model": ""}, {"model": 123}, {"modle": "example"}])
def test_headless_seats_reject_invalid_model_options(tmp_path: Path, bot: str, options: dict[str, Any]) -> None:
    with pytest.raises(SeatConstructionError) as error:
        prepare_seats(
            [PlayerConfig(bot=bot, options=options), PlayerConfig(bot="lowest_card")],
            seed=66,
            directory=tmp_path,
            rules=GameRules(),
            protocol=MatchProtocol(),
            session_options=SessionOptions(),
        )
    assert error.value.seat == 0
    assert isinstance(error.value.cause, ValueError)


@pytest.mark.parametrize("bot", ["codex-headless", "claude-headless"])
@pytest.mark.parametrize("effort", ["", " ", 123, False])
def test_headless_seats_reject_invalid_reasoning_options(tmp_path: Path, bot: str, effort: Any) -> None:
    with pytest.raises(SeatConstructionError) as error:
        prepare_seats(
            [PlayerConfig(bot=bot, options={"reasoning_effort": effort}), PlayerConfig(bot="lowest_card")],
            seed=66,
            directory=tmp_path,
            rules=GameRules(),
            protocol=MatchProtocol(),
            session_options=SessionOptions(),
        )
    assert error.value.seat == 0
    assert isinstance(error.value.cause, ValueError)


def test_custom_command_seat_rejects_reasoning_effort_even_when_null(tmp_path: Path) -> None:
    with pytest.raises(SeatConstructionError, match="reasoning_effort") as error:
        prepare_seats(
            [
                PlayerConfig(bot="command", options={"command": ["example-agent"], "reasoning_effort": None}),
                PlayerConfig(bot="lowest_card"),
            ],
            seed=66,
            directory=tmp_path,
            rules=GameRules(),
            protocol=MatchProtocol(),
            session_options=SessionOptions(),
        )
    assert error.value.seat == 0
    assert isinstance(error.value.cause, ValueError)


def test_cli_file_preserves_arena_settings_and_writes_replayable_match(config_file: Path, tmp_path: Path) -> None:
    directory = tmp_path / "run"
    result = runner.invoke(app, table_arguments(config_file, directory))
    assert result.exit_code == 0, result.output
    run = load_run(directory)
    assert run.plan.seed == 731
    assert run.plan.rules.target_score == 99
    assert run.plan.protocol.hands == 1
    assert run.plan.protocol.communication_enabled is True
    assert run.plan.execution.scheduler == "round_robin"
    assert run.plan.execution.match_action_limit == 400
    assert run.plan.execution.play_action_limit == 45
    assert run.plan.execution.decision_rejection_limit == 6
    assert run.plan.execution.decision_timeout_seconds == 5
    assert run.plan.execution.max_abandoned_decisions == 0
    catalogue = {entry.config_id: entry for entry in run.plan.catalogue}
    assert catalogue[run.results[0].seats[0].config_id].options["K"] == 3
    trace = run.results[0].event_trace
    assert trace is not None
    replay = runner.invoke(app, ["replay", str(directory / trace)])
    assert replay.exit_code == 0, replay.output
    assert "status=finished" in replay.output


def test_watch_preserves_seeded_fixed_lineup_results(config_file: Path, tmp_path: Path) -> None:
    watched_directory = tmp_path / "watched"
    watched = runner.invoke(app, table_arguments(config_file, watched_directory))
    unattended_directory = tmp_path / "unattended"
    unattended = runner.invoke(
        app, ["play", "--config", str(config_file), "--output-dir", str(unattended_directory), "--trace"]
    )
    assert watched.exit_code == 0, watched.output
    assert unattended.exit_code == 0, unattended.output
    watched_run = load_run(watched_directory)
    unattended_run = load_run(unattended_directory)
    assert watched_run.plan.jobs == unattended_run.plan.jobs
    assert [record.scores for record in watched_run.results] == [record.scores for record in unattended_run.results]
    assert [record.winners for record in watched_run.results] == [record.winners for record in unattended_run.results]


def test_watch_can_present_a_sampled_schedule(tmp_path: Path) -> None:
    config_file = tmp_path / "run.json"
    config_file.write_text(
        json.dumps({
            "catalogue": [{"bot": "lowest_card"}],
            "games": 1,
            "protocol": {"end_condition": "fixed_hands", "hands": 1},
        })
    )
    directory = tmp_path / "run"
    result = runner.invoke(app, table_arguments(config_file, directory))
    assert result.exit_code == 0, result.output
    run = load_run(directory)
    assert len(run.results) == 1
    assert run.plan.player_counts == (4,)
    assert len(run.results[0].seats) == 4


@pytest.mark.parametrize("bot", ["codex-headless", "claude-headless"])
def test_cli_preserves_catalogue_model_and_effort_through_headless_repair_and_records_acceptance(
    tmp_path: Path, bot: str
) -> None:
    script = tmp_path / "fake_harness.py"
    script.write_text(
        "import json, pathlib, sys, tomllib, uuid\n"
        "assert sys.argv[sys.argv.index('--model') + 1] == 'configured-model'\n"
        "request = json.loads(sys.stdin.read().splitlines()[-1])\n"
        "offer = request['offer']\n"
        "proposal = {key: offer[key] for key in ('protocol_version', 'session_id', 'decision_id', 'view_id')}\n"
        "proposal.update(submission_id=uuid.uuid4().hex, memory=None, "
        "actions=[{'type': 'select_card', 'card': min(offer['view']['you']['hand'])}])\n"
        "if 'protocol_error' not in offer:\n"
        "    proposal['actions'] = [{'type': 'commit'}]\n"
        "if '--output-last-message' in sys.argv:\n"
        "    assert tomllib.loads(sys.argv[sys.argv.index('-c') + 1])['model_reasoning_effort'] == 'high'\n"
        "    path = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "    path.write_text(json.dumps(proposal))\n"
        "else:\n"
        "    assert sys.argv[sys.argv.index('--effort') + 1] == 'high'\n"
        "    print(json.dumps({'subtype': 'success', 'is_error': False, 'structured_output': proposal}))\n",
        encoding="utf-8",
    )
    settings = {
        "catalogue": [
            {
                "key": "agent",
                "bot": bot,
                "options": {
                    "model": "configured-model",
                    "reasoning_effort": "high",
                    "command": [sys.executable, str(script)],
                },
            },
            {"bot": "lowest_card"},
        ],
        "lineup": ["agent", "lowest_card"],
        "execution": {"match_action_limit": 1},
    }
    config_file = tmp_path / "table.json"
    config_file.write_text(json.dumps(settings), encoding="utf-8")
    directory = tmp_path / "run"
    result = runner.invoke(app, table_arguments(config_file, directory))
    assert result.exit_code == 1, result.output
    run = load_run(directory)
    record = run.results[0]
    assert record.action_trace is not None
    actions = [json.loads(line) for line in (directory / record.action_trace).read_text().splitlines()]
    assert len(actions) == 1
    assert actions[0]["player_id"] == "player_1"
    assert actions[0]["outcome"] == "accepted"
    catalogue = {entry.config_id: entry for entry in run.plan.catalogue}
    assert catalogue[record.seats[0].config_id].options["model"] == "configured-model"
    assert catalogue[record.seats[0].config_id].options["reasoning_effort"] == "high"
    stats = record.seat_stats[0]
    assert stats is not None
    assert stats["invocations"] == 2


def test_explicit_cli_settings_override_table_file(table_settings: dict[str, Any], tmp_path: Path) -> None:
    table_settings["protocol"]["communication_enabled"] = False
    table_settings["execution"]["match_action_limit"] = 1
    config_file = tmp_path / "table.json"
    config_file.write_text(json.dumps(table_settings), encoding="utf-8")
    directory = tmp_path / "run"
    result = runner.invoke(
        app,
        [
            *table_arguments(config_file, directory),
            "--seed",
            "1234",
            "--hands",
            "2",
            "--communication",
            "--match-action-limit",
            "800",
            "--decision-timeout",
            "7",
        ],
    )
    assert result.exit_code == 0, result.output
    run = load_run(directory)
    assert run.plan.seed == 1234
    assert run.plan.protocol.hands == 2
    assert run.plan.protocol.communication_enabled is True
    assert run.plan.execution.match_action_limit == 800
    assert run.plan.execution.decision_timeout_seconds == 7
    assert run.results[0].outcome == "finished"


def test_configured_action_limit_stops_play_without_cli_override(
    table_settings: dict[str, Any], tmp_path: Path
) -> None:
    table_settings["execution"]["match_action_limit"] = 1
    config_file = tmp_path / "table.json"
    config_file.write_text(json.dumps(table_settings), encoding="utf-8")
    directory = tmp_path / "run"
    result = runner.invoke(app, table_arguments(config_file, directory))
    assert result.exit_code == 1, result.output
    record = load_run(directory).results[0]
    assert record.outcome == "abandoned"
    assert record.reason == "match_action_limit"


def test_cli_seat_keys_replace_the_entire_configured_lineup(config_file: Path, tmp_path: Path) -> None:
    directory = tmp_path / "run"
    result = runner.invoke(
        app,
        [*table_arguments(config_file, directory), "--seat", "careful", "--seat", "careful", "--seat", "lowest_card"],
    )
    assert result.exit_code == 0, result.output
    run = load_run(directory)
    catalogue = {entry.config_id: entry for entry in run.plan.catalogue}
    lineup = [catalogue[seat.config_id] for seat in run.results[0].seats]
    assert [entry.bot for entry in lineup] == ["controlled_burn", "controlled_burn", "lowest_card"]
    assert [entry.options["K"] for entry in lineup[:2]] == [3, 3]


def test_cli_seat_key_takes_precedence_over_registered_bot_name(table_settings: dict[str, Any], tmp_path: Path) -> None:
    table_settings["catalogue"][0]["key"] = "lowest_card"
    table_settings["lineup"] = ["lowest_card", "highest_card"]
    config_file = tmp_path / "table.json"
    config_file.write_text(json.dumps(table_settings), encoding="utf-8")
    directory = tmp_path / "run"
    result = runner.invoke(
        app, [*table_arguments(config_file, directory), "--seat", "lowest_card", "--seat", "highest_card"]
    )
    assert result.exit_code == 0, result.output
    run = load_run(directory)
    catalogue = {entry.config_id: entry for entry in run.plan.catalogue}
    assert catalogue[run.results[0].seats[0].config_id].bot == "controlled_burn"


@pytest.mark.parametrize(
    "contents",
    [
        "not json",
        '{"catalogue":[{"bot":"lowest_card"}],"lineup":["lowest_card","missing"]}',
        '{"catalogue":[{"bot":"lowest_card","options":{"typo":1}}],"lineup":["lowest_card","lowest_card"]}',
        '{"catalogue":[{"bot":"unknown"}],"lineup":["unknown","unknown"]}',
        '{"catalogue":[{"bot":"lowest_card"}],"lineup":["lowest_card","lowest_card"],"execution":{"concurrency":2}}',
    ],
)
def test_cli_rejects_invalid_configuration_before_creating_output(tmp_path: Path, contents: str) -> None:
    config_file = tmp_path / "table.json"
    config_file.write_text(contents, encoding="utf-8")
    directory = tmp_path / "run"
    result = runner.invoke(app, table_arguments(config_file, directory))
    assert result.exit_code == 2, result.output
    assert not directory.exists()
