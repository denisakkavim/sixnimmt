"""Each seat owns validated bot options, identity, and reproducible provenance."""

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field
from typer.testing import CliRunner

from sixnimmt.arena.bots.base import BotOptions, BotSpec
from sixnimmt.arena.bots.heuristics import RandomBot
from sixnimmt.arena.bots.registry import REGISTRY
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.players import PlayerConfig, resolve_players, resolve_strategy
from sixnimmt.cli import app
from sixnimmt.engine.audience import Viewer, visible_events
from sixnimmt.engine.events import Event
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.engine.state import MatchState
from sixnimmt.engine.views import ViewRole
from sixnimmt.persistence.sink import read_event_log
from tests.run_helpers import run_fixed


class StrategyOptions(BotOptions):
    temperature: float = Field(default=0.5, ge=0, le=2)
    prompt: str = "Play carefully"
    tags: list[str] = Field(default_factory=list)


@pytest.fixture
def short_protocol() -> MatchProtocol:
    return MatchProtocol(end_condition="fixed_hands", hands=1)


def test_per_player_options_reach_fresh_factories_and_traces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, short_protocol: MatchProtocol
) -> None:
    calls: list[tuple[float, str, list[str]]] = []
    lock = threading.Lock()

    def build(seed: int, *, temperature: float, prompt: str, tags: list[str]) -> RandomBot:
        with lock:
            calls.append((temperature, prompt, list(tags)))
        tags.append("mutated by factory")
        return RandomBot(seed)

    monkeypatch.setitem(REGISTRY, "strategy", BotSpec("strategy", build, True, {"version": "1"}, StrategyOptions))
    first = PlayerConfig(
        bot="strategy",
        display_name="Alice",
        options={"temperature": 0.2, "prompt": "First", "tags": ["first"]},
    )
    second = PlayerConfig(bot="strategy", display_name="Bob", options={"prompt": "Second"})
    directory = tmp_path / "trace"
    result = run_fixed(
        [first, second],
        4,
        123,
        protocol=short_protocol,
        config=RunConfig(concurrency=3),
        output_dir=directory,
        trace=True,
    )
    assert len(result.results) == 4
    assert all(record.outcome == "finished" for record in result.results)
    assert [entry.label for entry in result.plan.catalogue] == ["Alice", "Bob"]
    assert calls.count((0.2, "First", ["first"])) == 4
    assert calls.count((0.5, "Second", [])) == 4
    assert first.options["tags"] == ["first"]
    assert result.plan.catalogue[1].options == {"temperature": 0.5, "prompt": "Second", "tags": []}
    assert result.plan.catalogue[0].metadata == {
        "version": "1",
        "bot_options": {"temperature": 0.2, "prompt": "First", "tags": ["first"]},
    }
    trace = result.results[0].event_trace
    assert trace is not None
    log = read_event_log(directory / trace)
    assert "First" not in json.dumps([
        event.model_dump(mode="json") for event in visible_events(log, Viewer(ViewRole.PLAYER, "player_2"))
    ])
    admin_created = next(event for event in log if event.type == "match_created" and event.audience == "admin")
    assert admin_created.data["players"][0]["agent_metadata"] == result.plan.catalogue[0].metadata


@pytest.mark.parametrize("options", [{"temperatur": 0.2}, {"temperature": -1.0}, {"temperature": "hot"}])
def test_invalid_options_fail_before_any_bot_is_constructed(
    monkeypatch: pytest.MonkeyPatch, options: dict[str, Any]
) -> None:
    builds = []

    def build(seed: int, **options: Any) -> RandomBot:
        builds.append(seed)
        return RandomBot(seed)

    monkeypatch.setitem(REGISTRY, "strategy", BotSpec("strategy", build, True, {}, StrategyOptions))
    with pytest.raises(ValueError, match="invalid options"):
        run_fixed([PlayerConfig(bot="strategy"), PlayerConfig(bot="strategy", options=options)], 1, 123)
    assert builds == []


def test_builtin_bots_reject_unsupported_options() -> None:
    with pytest.raises(ValueError, match="invalid options"):
        run_fixed(
            [PlayerConfig(bot="random", options={"model": "unsupported"}), PlayerConfig(bot="lowest_fitting_card")],
            1,
            123,
        )


def test_name_only_python_api_is_rejected() -> None:
    players: Any = ["random", "lowest_fitting_card"]
    with pytest.raises(TypeError, match="PlayerConfig"):
        resolve_players(players)


def test_configured_names_reach_observer_without_changing_seat_ids(short_protocol: MatchProtocol) -> None:
    identities = []

    def observe(state: MatchState, events: tuple[Event, ...]) -> None:
        if len(identities) == 0:
            identities.extend((seat.player_id, seat.display_name) for seat in state.players)

    run_fixed(
        [PlayerConfig(bot="random", display_name="Alice"), PlayerConfig(bot="lowest_fitting_card", display_name="Bob")],
        1,
        123,
        protocol=short_protocol,
        observer=observe,
    )
    assert identities == [("player_1", "Alice"), ("player_2", "Bob")]


def test_cli_passes_validated_options_to_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    options_received = []

    def build(seed: int, *, temperature: float, prompt: str, tags: list[str]) -> RandomBot:
        options_received.append((temperature, prompt, tags))
        return RandomBot(seed)

    monkeypatch.setitem(REGISTRY, "strategy", BotSpec("strategy", build, True, {}, StrategyOptions))
    path = tmp_path / "arena.json"
    path.write_text(
        json.dumps({
            "catalogue": [
                {"key": "alice", "bot": "strategy", "label": "Alice", "options": {"temperature": 0.2}},
                {"key": "bob", "bot": "strategy", "label": "Bob", "options": {"temperature": 0.8}},
            ],
            "player_counts": [2],
            "games": 0,
            "controlled_games": 1,
            "controlled_coverage": "explicit",
            "compositions": [["alice", "bob"]],
        })
    )
    result = CliRunner().invoke(
        app, ["play", "--config", str(path), "--seed", "123", "--output-dir", str(tmp_path / "run")]
    )
    assert result.exit_code == 0, result.output
    assert sorted(options_received) == [(0.2, "Play carefully", []), (0.8, "Play carefully", [])]
    assert "Alice" in result.stdout
    assert "Bob" in result.stdout


def test_typed_factory_gets_independent_validated_options(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[list[str]] = []

    def build(seed: int, options: StrategyOptions) -> RandomBot:
        received.append(list(options.tags))
        options.tags.append("factory mutation")
        return RandomBot(seed)

    spec = BotSpec.typed("typed", StrategyOptions, build, True, {"version": "1"})
    monkeypatch.setitem(REGISTRY, "typed", spec)
    strategy = resolve_strategy("typed", {"tags": ["original"]})
    first = strategy.build(1)
    second = strategy.build(1)
    assert first is not second
    assert received == [["original"], ["original"]]
    assert strategy.recorded_options["tags"] == ["original"]
    assert isinstance(strategy.options, StrategyOptions)
    assert strategy.options.tags == ["original"]


def test_resolved_strategy_keeps_seat_annotations_separate() -> None:
    player = resolve_players([
        PlayerConfig(bot="lowest_card", display_name="Alice", agent_metadata={"group": "control"})
    ])[0]
    assert player.config.display_name == "Alice"
    assert player.metadata["group"] == "control"
    assert "group" not in player.strategy.metadata
    assert player.strategy.name == "lowest_card"
