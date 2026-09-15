"""Watched tables share the arena catalogue vocabulary and option precedence."""

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sixnimmt.arena.bots.external import ManagedHarnessBot
from sixnimmt.arena.bots.llm import LLMBot
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.arena.table import TableConfig, create_seats
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
        "table",
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
    players = TableConfig.model_validate(table_settings).players()
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
        {"execution": {"backend": "process"}},
        {"execution": {"concurrency": 2}},
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
        "process-backend",
        "concurrent-tables",
    ],
)
def test_invalid_table_configuration_is_rejected(table_settings: dict[str, Any], updates: dict[str, Any]) -> None:
    table_settings.update(updates)
    with pytest.raises(ValueError):
        TableConfig.model_validate(table_settings).players()


def test_configuration_requires_a_lineup_before_constructing_players() -> None:
    settings = TableConfig.model_validate({"catalogue": [{"bot": "lowest_card"}]})
    with pytest.raises(ValueError):
        settings.players()


def test_configuration_without_lineup_can_select_catalogue_keys_from_cli() -> None:
    settings = TableConfig.model_validate({
        "catalogue": [{"key": "small", "bot": "lowest_card"}],
        "seed": 731,
    })
    players = settings.players(["small", "highest_card"])
    assert [player.bot for player in players] == ["lowest_card", "highest_card"]


def test_custom_command_profile_can_be_defined_inside_catalogue(tmp_path: Path) -> None:
    settings = TableConfig.model_validate({
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
    seats = create_seats(
        settings.players(), seed=66, directory=tmp_path, rules=settings.rules, protocol=settings.protocol
    )
    driver = seats[0].driver
    assert driver is not None
    assert driver.profile.command == ("example-agent", "--json")
    assert driver.profile.timeout_seconds == 15
    assert driver.profile.max_output_bytes == 4096


def test_llm_and_headless_models_use_the_same_catalogue_options_field(tmp_path: Path) -> None:
    settings = TableConfig.model_validate({
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
    seats = create_seats(
        settings.players(), seed=66, directory=tmp_path, rules=settings.rules, protocol=settings.protocol
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
    recorded = seats[2].seat.agent_metadata["bot_options"]
    assert isinstance(recorded, dict)
    assert recorded["model"] == "api-example"


def test_repeated_catalogue_key_constructs_independent_headless_sessions(tmp_path: Path) -> None:
    settings = TableConfig.model_validate({
        "catalogue": [{"key": "agent", "bot": "codex-headless", "options": {"model": "example"}}],
        "lineup": ["agent", "agent"],
    })
    first, second = create_seats(
        settings.players(), seed=66, directory=tmp_path, rules=settings.rules, protocol=settings.protocol
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
    with pytest.raises(ValueError, match="native"):
        create_seats(
            [PlayerConfig(bot=bot, options={option: "example"}), PlayerConfig(bot="lowest_card")],
            seed=66,
            directory=tmp_path,
            rules=GameRules(),
            protocol=MatchProtocol(),
        )


@pytest.mark.parametrize("bot", ["codex-headless", "claude-headless"])
@pytest.mark.parametrize("options", [{"model": ""}, {"model": 123}, {"modle": "example"}])
def test_headless_seats_reject_invalid_model_options(tmp_path: Path, bot: str, options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        create_seats(
            [PlayerConfig(bot=bot, options=options), PlayerConfig(bot="lowest_card")],
            seed=66,
            directory=tmp_path,
            rules=GameRules(),
            protocol=MatchProtocol(),
        )


@pytest.mark.parametrize("bot", ["codex-headless", "claude-headless"])
@pytest.mark.parametrize("effort", ["", " ", 123, False])
def test_headless_seats_reject_invalid_reasoning_options(tmp_path: Path, bot: str, effort: Any) -> None:
    with pytest.raises(ValueError):
        create_seats(
            [PlayerConfig(bot=bot, options={"reasoning_effort": effort}), PlayerConfig(bot="lowest_card")],
            seed=66,
            directory=tmp_path,
            rules=GameRules(),
            protocol=MatchProtocol(),
        )


def test_custom_command_seat_rejects_reasoning_effort_even_when_null(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        create_seats(
            [
                PlayerConfig(bot="command", options={"command": ["example-agent"], "reasoning_effort": None}),
                PlayerConfig(bot="lowest_card"),
            ],
            seed=66,
            directory=tmp_path,
            rules=GameRules(),
            protocol=MatchProtocol(),
        )


def test_cli_file_preserves_arena_settings_and_writes_replayable_match(config_file: Path, tmp_path: Path) -> None:
    directory = tmp_path / "run"
    result = runner.invoke(app, table_arguments(config_file, directory))
    assert result.exit_code == 0, result.output
    manifest = json.loads((directory / "traces" / "manifest.json").read_text())
    assert manifest["seed"] == 731
    assert manifest["rules"]["target_score"] == 99
    assert manifest["protocol"]["hands"] == 1
    assert manifest["protocol"]["communication_enabled"] is True
    assert manifest["run_config"]["scheduler"] == "round_robin"
    assert manifest["run_config"]["match_action_limit"] == 400
    assert manifest["run_config"]["play_action_limit"] == 45
    assert manifest["run_config"]["decision_rejection_limit"] == 6
    assert manifest["run_config"]["decision_timeout_seconds"] == 5
    assert manifest["run_config"]["max_abandoned_decisions"] == 0
    assert manifest["seats"][0]["agent_metadata"]["bot_options"]["K"] == 3
    replay = runner.invoke(app, ["replay", str(directory / "traces" / "table.jsonl")])
    assert replay.exit_code == 0, replay.output
    assert "status=finished" in replay.output


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
    assert result.exit_code == 0, result.output
    actions = [json.loads(line) for line in (directory / "traces" / "table.actions.jsonl").read_text().splitlines()]
    assert len(actions) == 1
    assert actions[0]["player_id"] == "player_1"
    assert actions[0]["outcome"] == "accepted"
    manifest = json.loads((directory / "traces" / "manifest.json").read_text())
    assert manifest["seats"][0]["agent_metadata"]["bot_options"]["model"] == "configured-model"
    assert manifest["seats"][0]["agent_metadata"]["bot_options"]["reasoning_effort"] == "high"
    assert manifest["matches"][0]["seat_stats"]["player_1"]["invocations"] == 2


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
    manifest = json.loads((directory / "traces" / "manifest.json").read_text())
    assert manifest["seed"] == 1234
    assert manifest["protocol"]["hands"] == 2
    assert manifest["protocol"]["communication_enabled"] is True
    assert manifest["run_config"]["match_action_limit"] == 800
    assert manifest["run_config"]["decision_timeout_seconds"] == 7
    assert manifest["matches"][0]["outcome"] == "finished"


def test_configured_action_limit_stops_play_without_cli_override(
    table_settings: dict[str, Any], tmp_path: Path
) -> None:
    table_settings["execution"]["match_action_limit"] = 1
    config_file = tmp_path / "table.json"
    config_file.write_text(json.dumps(table_settings), encoding="utf-8")
    directory = tmp_path / "run"
    result = runner.invoke(app, table_arguments(config_file, directory))
    assert result.exit_code == 0, result.output
    summary = json.loads((directory / "result.json").read_text())
    assert summary["outcome"] == "abandoned"
    assert summary["reason"] == "match_action_limit"


def test_cli_seat_keys_replace_the_entire_configured_lineup(config_file: Path, tmp_path: Path) -> None:
    directory = tmp_path / "run"
    result = runner.invoke(
        app,
        [*table_arguments(config_file, directory), "--seat", "careful", "--seat", "careful", "--seat", "lowest_card"],
    )
    assert result.exit_code == 0, result.output
    manifest = json.loads((directory / "traces" / "manifest.json").read_text())
    assert [seat["bot"] for seat in manifest["seats"]] == [
        "ControlledBurnBot",
        "ControlledBurnBot",
        "LowestCardBot",
    ]
    assert [seat["agent_metadata"]["bot_options"]["K"] for seat in manifest["seats"][:2]] == [3, 3]


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
    manifest = json.loads((directory / "traces" / "manifest.json").read_text())
    assert manifest["seats"][0]["bot"] == "ControlledBurnBot"


@pytest.mark.parametrize(
    "contents",
    [
        "not json",
        '{"catalogue":[{"bot":"lowest_card"}]}',
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
