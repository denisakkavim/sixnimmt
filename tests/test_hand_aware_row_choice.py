"""Affordable row shaping with unchanged card selection."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from sixnimmt.arena.bots import (
    REGISTRY,
    ActionBatch,
    BotSpec,
    HandAwareRowChoiceBot,
    HandAwareRowChoiceOptions,
    Rejection,
)
from sixnimmt.arena.bots.highest_card import HighestCardBot
from sixnimmt.arena.players import PlayerConfig, resolve_players
from sixnimmt.arena.runner import RunConfig, run_arena
from sixnimmt.engine.actions import ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.audience import Viewer
from sixnimmt.engine.errors import ErrorCode
from sixnimmt.engine.fold import build_view
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.views import MatchView, RowView, ViewRole


@pytest.fixture
def observation() -> MatchView:
    _, events = create_match("row-shaping", ["a", "b"], 123)
    view = build_view(events, Viewer(ViewRole.PLAYER, "a"))
    rows = ((10,), (21, 23, 26, 28, 29), (60,), (90,))
    return view.model_copy(
        update={
            "rows": tuple(RowView(index=index, cards=cards) for index, cards in enumerate(rows)),
            "you": view.you.model_copy(update={"hand": (30, 31, 32)}),
            "awaiting_card": 5,
            "legal_actions": ("choose_row",),
        }
    )


@pytest.mark.parametrize("limit, expected", [(0, 0), (1, 0), (2, 1), (10, 1)])
def test_extra_penalty_limit_is_inclusive(observation: MatchView, limit: int, expected: int) -> None:
    assert HandAwareRowChoiceBot(limit, HighestCardBot()).act(observation) == ChooseRowAction(row_index=expected)


def test_equal_fit_counts_prefer_cheapest_row(observation: MatchView) -> None:
    view = observation.model_copy(update={"you": observation.you.model_copy(update={"hand": (7,)})})
    assert HandAwareRowChoiceBot(10, HighestCardBot()).act(view) == ChooseRowAction(row_index=0)


def test_equal_cost_and_fit_counts_prefer_row_index(observation: MatchView) -> None:
    view = observation.model_copy(
        update={"rows": tuple(reversed(observation.rows)), "you": observation.you.model_copy(update={"hand": (7,)})}
    )
    assert HandAwareRowChoiceBot(10, HighestCardBot()).act(view) == ChooseRowAction(row_index=0)


def test_last_card_uses_cheapest_row(observation: MatchView) -> None:
    view = observation.model_copy(update={"you": observation.you.model_copy(update={"hand": ()})})
    assert HandAwareRowChoiceBot(10, HighestCardBot()).act(view) == ChooseRowAction(row_index=0)


def test_actual_awaiting_card_determines_replacement_value(observation: MatchView) -> None:
    rows = (RowView(index=0, cards=(4, 6, 7, 8, 9)), *observation.rows[1:])
    view = observation.model_copy(
        update={"rows": rows, "awaiting_card": 2, "you": observation.you.model_copy(update={"hand": (3,)})}
    )
    assert HandAwareRowChoiceBot(10, HighestCardBot()).act(view) == ChooseRowAction(row_index=2)


def test_row_evaluation_does_not_mutate_observation(observation: MatchView) -> None:
    before = observation.model_dump()
    HandAwareRowChoiceBot(2, HighestCardBot()).act(observation)
    assert observation.model_dump() == before


def test_missing_awaiting_card_is_rejected(observation: MatchView) -> None:
    view = observation.model_copy(update={"awaiting_card": None})
    with pytest.raises(ValueError, match="awaiting card"):
        HandAwareRowChoiceBot(2, HighestCardBot()).act(view)


class RecordingCardBot:
    def __init__(self) -> None:
        self.received: tuple[MatchView, Rejection | None] | None = None
        self.proposal = ActionBatch(actions=(SelectCardAction(card=32), CommitAction()))

    def act(self, view: MatchView, rejection: Rejection | None = None) -> ActionBatch:
        self.received = (view, rejection)
        return self.proposal


def test_card_proposals_and_feedback_are_delegated_unchanged(observation: MatchView) -> None:
    view = observation.model_copy(update={"legal_actions": ("select_card",)})
    rejection = Rejection(ErrorCode.WRONG_PHASE, "retry", view.legal_actions)
    delegate = RecordingCardBot()
    assert HandAwareRowChoiceBot(2, delegate).act(view, rejection) is delegate.proposal
    assert delegate.received == (view, rejection)


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"max_extra_penalty": 2},
        {"card_strategy": "random"},
        {"max_extra_penalty": -1, "card_strategy": "random"},
        {"max_extra_penalty": True, "card_strategy": "random"},
    ],
)
def test_configuration_requires_explicit_valid_policy(options: dict) -> None:
    with pytest.raises(ValidationError):
        HandAwareRowChoiceOptions.model_validate(options)


@pytest.mark.parametrize("strategy, options", [("missing", {}), ("random", {"typo": 1})])
def test_card_strategy_is_validated_before_running(strategy: str, options: dict) -> None:
    with pytest.raises(ValueError):
        resolve_players([
            PlayerConfig(
                bot="hand_aware_row_choice",
                options={
                    "max_extra_penalty": 2,
                    "card_strategy": strategy,
                    "card_options": options,
                },
            )
        ])


@pytest.mark.parametrize("communication", [False, True])
def test_nested_card_strategy_is_reproducible_across_backends(communication: bool, tmp_path: Path) -> None:
    players = [
        PlayerConfig(
            bot="hand_aware_row_choice",
            options={
                "max_extra_penalty": 2,
                "card_strategy": "controlled_burn",
                "card_options": {"K": 3, "fallback_strategy": "random"},
            },
        ),
        PlayerConfig(bot="closest_gap"),
    ]
    protocol = MatchProtocol(communication_enabled=communication, end_condition="fixed_hands", hands=2)
    first = run_arena(players, 4, 123, protocol=protocol)
    directory = tmp_path / "trace"
    second = run_arena(
        players, 4, 123, protocol=protocol, config=RunConfig(backend="process", concurrency=2, trace_dir=directory)
    )
    assert first == second
    assert second.finished == 4
    assert second.reproducible
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["seats"][0]["options"]["card_options"] == {
        "K": 3,
        "fallback_strategy": "random",
        "fallback_options": {},
    }


def build_highest(seed: int) -> HighestCardBot:
    return HighestCardBot()


@pytest.mark.parametrize("deterministic", [False, True])
def test_custom_card_strategy_is_resolved_in_parent(monkeypatch: pytest.MonkeyPatch, deterministic: bool) -> None:
    monkeypatch.setitem(REGISTRY, "custom", BotSpec("custom", build_highest, deterministic, {"version": "custom"}))
    players = [
        PlayerConfig(bot="hand_aware_row_choice", options={"max_extra_penalty": 2, "card_strategy": "custom"}),
        PlayerConfig(bot="random"),
    ]
    result = run_arena(
        players,
        2,
        123,
        protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
        config=RunConfig(backend="process"),
    )
    assert result.finished == 2
    assert result.reproducible is deterministic
