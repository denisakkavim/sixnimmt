"""Each seat owns validated bot options, identity, and reproducible provenance."""

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

from sixnimmt.arena.bots import REGISTRY, BotOptions, BotSpec, RandomBot
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.arena.runner import RunConfig, run_arena
from sixnimmt.engine.audience import Viewer, visible_events
from sixnimmt.engine.events import Event
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.engine.state import MatchState
from sixnimmt.engine.views import ViewRole
from sixnimmt.persistence.sink import read_event_log


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
        agent_metadata={"experiment_group": "control"},
    )
    second = PlayerConfig(bot="strategy", display_name="Bob", options={"prompt": "Second"})
    directory = tmp_path / "trace"
    result = run_arena(
        [first, second], 4, 123, protocol=short_protocol, config=RunConfig(concurrency=3, trace_dir=directory)
    )
    assert result.finished == 4
    assert [seat.display_name for seat in result.players] == ["Alice", "Bob"]
    assert calls.count((0.2, "First", ["first"])) == 4
    assert calls.count((0.5, "Second", [])) == 4
    assert first.options["tags"] == ["first"]
    manifest = json.loads((directory / "manifest.json").read_text())
    assert [seat["display_name"] for seat in manifest["seats"]] == ["Alice", "Bob"]
    assert manifest["seats"][1]["options"] == {"temperature": 0.5, "prompt": "Second", "tags": []}
    assert manifest["seats"][0]["agent_metadata"] == {
        "version": "1",
        "experiment_group": "control",
        "bot_options": {"temperature": 0.2, "prompt": "First", "tags": ["first"]},
    }
    log = read_event_log(directory / manifest["matches"][0]["log"])
    assert "First" not in json.dumps([
        event.model_dump(mode="json") for event in visible_events(log, Viewer(ViewRole.PLAYER, "player_2"))
    ])
    admin_created = next(event for event in log if event.type == "match_created" and event.audience == "admin")
    assert admin_created.data["players"][0]["agent_metadata"] == manifest["seats"][0]["agent_metadata"]


@pytest.mark.parametrize("options", [{"temperatur": 0.2}, {"temperature": -1.0}, {"temperature": "hot"}])
def test_invalid_options_fail_before_any_bot_is_constructed(
    monkeypatch: pytest.MonkeyPatch, options: dict[str, Any]
) -> None:
    builds = []

    def build(seed: int, **options: Any) -> RandomBot:
        builds.append(seed)
        return RandomBot(seed)

    monkeypatch.setitem(REGISTRY, "strategy", BotSpec("strategy", build, True, {}, StrategyOptions))
    with pytest.raises(ValueError, match="invalid options for player 2"):
        run_arena([PlayerConfig(bot="strategy"), PlayerConfig(bot="strategy", options=options)], 1, 123)
    assert builds == []


def test_builtin_bots_reject_unsupported_options() -> None:
    with pytest.raises(ValueError, match="invalid options for player 1"):
        run_arena(
            [PlayerConfig(bot="random", options={"model": "unsupported"}), PlayerConfig(bot="lowest_fitting_card")],
            1,
            123,
        )


def test_name_only_python_api_is_rejected() -> None:
    players: Any = ["random", "lowest_fitting_card"]
    with pytest.raises(TypeError, match="PlayerConfig"):
        run_arena(players, 1, 123)


def test_configured_names_reach_observer_without_changing_seat_ids(short_protocol: MatchProtocol) -> None:
    identities = []

    def observe(state: MatchState, events: tuple[Event, ...]) -> None:
        if not identities:
            identities.extend((seat.player_id, seat.display_name) for seat in state.players)

    run_arena(
        [PlayerConfig(bot="random", display_name="Alice"), PlayerConfig(bot="lowest_fitting_card", display_name="Bob")],
        1,
        123,
        protocol=short_protocol,
        observer=observe,
    )
    assert identities == [("player_1", "Alice"), ("player_2", "Bob")]


def test_cli_passes_validated_options_to_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from sixnimmt.cli import app

    options_received = []

    def build(seed: int, *, temperature: float, prompt: str, tags: list[str]) -> RandomBot:
        options_received.append((temperature, prompt, tags))
        return RandomBot(seed)

    monkeypatch.setitem(REGISTRY, "strategy", BotSpec("strategy", build, True, {}, StrategyOptions))
    path = tmp_path / "players.json"
    path.write_text(
        json.dumps([
            {"bot": "strategy", "display_name": "Alice", "options": {"temperature": 0.2}},
            {"bot": "strategy", "display_name": "Bob", "options": {"temperature": 0.8}},
        ])
    )
    result = CliRunner().invoke(app, ["arena", "--players-file", str(path), "--games", "1", "--seed", "123"])
    assert result.exit_code == 0, result.output
    assert options_received == [(0.2, "Play carefully", []), (0.8, "Play carefully", [])]
    assert "Alice" in result.stdout
    assert "Bob" in result.stdout
