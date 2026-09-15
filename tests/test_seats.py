"""Shared seat preparation preserves seeds, ownership, and construction failures."""

import sys
from pathlib import Path

import pytest

from sixnimmt.arena.bots.base import BotOptions, BotSpec, Rejection
from sixnimmt.arena.bots.heuristics import RandomBot
from sixnimmt.arena.bots.lifecycle import BotMatchEnd, close_bot
from sixnimmt.arena.bots.registry import REGISTRY
from sixnimmt.arena.config import SessionOptions
from sixnimmt.arena.players import PlayerConfig, resolve_strategy
from sixnimmt.arena.sessions import (
    SeatConstructionError,
    prepare_seats,
    resolve_external_seat,
)
from sixnimmt.engine.actions import Action
from sixnimmt.engine.audience import Viewer
from sixnimmt.engine.fold import build_view
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.views import MatchView, ViewRole


class ClosableBot:
    def __init__(self) -> None:
        self.closed = 0

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        return RandomBot(1).act(view, rejection)

    def close(self, result: BotMatchEnd | None) -> None:
        self.closed += 1


def test_explicit_seeds_preserve_schedule_randomness(tmp_path: Path) -> None:
    seats = prepare_seats(
        [PlayerConfig(bot="random"), PlayerConfig(bot="random")],
        seed=66,
        directory=tmp_path,
        rules=GameRules(),
        protocol=MatchProtocol(),
        session_options=SessionOptions(),
        bot_seeds=(501, 502),
    )
    _, events = create_match("seat-seeds", ["player_1", "player_2"], 123)
    view = build_view(events, Viewer(ViewRole.PLAYER, "player_1"))
    for seat, seed in zip(seats, (501, 502), strict=True):
        expected = RandomBot(seed)
        assert [seat.bot.act(view) for _ in range(20)] == [expected.act(view) for _ in range(20)]


def test_supplied_strategy_does_not_require_worker_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    strategy = resolve_strategy("random", {})
    monkeypatch.delitem(REGISTRY, "random")
    seats = prepare_seats(
        [PlayerConfig(bot="random"), PlayerConfig(bot="random")],
        seed=66,
        directory=tmp_path,
        rules=GameRules(),
        protocol=MatchProtocol(),
        session_options=SessionOptions(),
        strategies=(strategy, strategy),
    )
    assert all(isinstance(seat.bot, RandomBot) for seat in seats)


def test_construction_failure_reports_seat_and_closes_previous_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = ClosableBot()

    def build(seed: int, options: BotOptions) -> ClosableBot:
        if seed == 68:
            msg = "second factory failed"
            raise ValueError(msg)
        return first

    monkeypatch.setitem(REGISTRY, "failing", BotSpec.typed("failing", BotOptions, build, True, {}))
    with pytest.raises(SeatConstructionError, match="cannot construct seat 2") as error:
        prepare_seats(
            [PlayerConfig(bot="failing"), PlayerConfig(bot="failing")],
            seed=66,
            directory=tmp_path,
            rules=GameRules(),
            protocol=MatchProtocol(),
            session_options=SessionOptions(),
        )
    assert error.value.seat == 1
    assert isinstance(error.value.cause, ValueError)
    assert first.closed == 1


def test_invalid_seat_options_preserve_validation_cause(tmp_path: Path) -> None:
    with pytest.raises(SeatConstructionError, match="native seats") as error:
        prepare_seats(
            [PlayerConfig(bot="codex", options={"invalid": True}), PlayerConfig(bot="random")],
            seed=66,
            directory=tmp_path,
            rules=GameRules(),
            protocol=MatchProtocol(),
            session_options=SessionOptions(),
        )
    assert error.value.seat == 0
    assert isinstance(error.value.cause, ValueError)


def test_external_metadata_excludes_command_but_keeps_construction_options() -> None:
    resolved = resolve_external_seat("command", {"command": [sys.executable, "-c", "print('private argument')"]})
    assert resolved.descriptor.ownership == "managed"
    assert not resolved.deterministic
    assert "command" in resolved.options
    assert "command" not in resolved.recorded_options
    assert "private argument" not in str(resolved.metadata)
    assert resolved.recorded_options["timeout_seconds"] is None


def test_external_seat_capabilities_describe_live_resources(tmp_path: Path) -> None:
    seats = prepare_seats(
        [PlayerConfig(bot="codex"), PlayerConfig(bot="command", options={"command": [sys.executable, "-c", "pass"]})],
        seed=66,
        directory=tmp_path,
        rules=GameRules(),
        protocol=MatchProtocol(),
        session_options=SessionOptions(),
    )
    try:
        assert [seat.ownership for seat in seats] == ["attached", "managed"]
        assert all(seat.requires_readiness and not seat.process_compatible for seat in seats)
        assert seats[1].worker is not None
        assert seats[1].worker.thread.ident is None
    finally:
        for seat in seats:
            close_bot(seat.bot, None)
