"""Controlled pickups and configurable fallback integration."""

import pytest
from pydantic import ValidationError

from sixnimmt.arena.bots import REGISTRY, BotOptions, BotSpec, ControlledBurnBot, ControlledBurnOptions
from sixnimmt.arena.bots.highest_card import HighestCardBot
from sixnimmt.arena.players import PlayerConfig, resolve_players
from sixnimmt.arena.runner import RunConfig, run_arena
from sixnimmt.engine.actions import ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.audience import Viewer
from sixnimmt.engine.fold import build_view
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.views import MatchView, RowView, ViewRole


@pytest.fixture
def observation() -> MatchView:
    _, events = create_match("burn", ["a", "b"], 123)
    view = build_view(events, Viewer(ViewRole.PLAYER, "a"))
    return view.model_copy(
        update={
            "rows": tuple(RowView(index=index, cards=(card,)) for index, card in enumerate((20, 30, 40, 50))),
            "you": view.you.model_copy(update={"hand": (5, 11, 12, 13, 60)}),
        }
    )


@pytest.mark.parametrize("K, card", [(0, 60), (2, 60), (3, 12), (8, 12)])
def test_burn_threshold_is_inclusive_and_minimises_card_heads(observation: MatchView, K: int, card: int) -> None:
    bot = ControlledBurnBot(K, HighestCardBot())
    assert bot.act(observation) == SelectCardAction(card=card)


def test_burn_uses_fallback_when_no_card_is_below_all_rows(observation: MatchView) -> None:
    view = observation.model_copy(update={"you": observation.you.model_copy(update={"hand": (25, 60)})})
    assert ControlledBurnBot(8, HighestCardBot()).act(view) == SelectCardAction(card=60)


def test_burn_rechecks_row_cost_and_breaks_row_ties_by_index(observation: MatchView) -> None:
    rows = (
        RowView(index=3, cards=(1,)),
        RowView(index=1, cards=(2,)),
        RowView(index=0, cards=(55,)),
        RowView(index=2, cards=(10,)),
    )
    view = observation.model_copy(update={"rows": rows, "legal_actions": ("choose_row",)})
    assert ControlledBurnBot(0, HighestCardBot()).act(view) == ChooseRowAction(row_index=1)


def test_burn_commits_its_selected_card(observation: MatchView) -> None:
    view = observation.model_copy(update={"legal_actions": ("select_card", "commit")})
    assert ControlledBurnBot(3, HighestCardBot()).act(view) == CommitAction()


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"K": 3},
        {"fallback_strategy": "closest_gap"},
        {"K": -1, "fallback_strategy": "random"},
        {"K": True, "fallback_strategy": "random"},
    ],
)
def test_burn_rejects_missing_or_invalid_required_settings(options: dict) -> None:
    with pytest.raises(ValidationError):
        ControlledBurnOptions.model_validate(options)


@pytest.mark.parametrize("fallback, options", [("missing", {}), ("closest_gap", {"typo": 1})])
def test_burn_validates_fallback_before_running(fallback: str, options: dict) -> None:
    with pytest.raises(ValueError):
        resolve_players([
            PlayerConfig(
                bot="controlled_burn",
                options={
                    "K": 3,
                    "fallback_strategy": fallback,
                    "fallback_options": options,
                },
            )
        ])


class FallbackOptions(BotOptions):
    prefer_high: bool = True


class ConfiguredFallback:
    def __init__(self, seed: int, *, prefer_high: bool) -> None:
        self.prefer_high = prefer_high

    def act(self, view: MatchView, rejection=None):
        card = max(view.you.hand) if self.prefer_high else min(view.you.hand)
        return SelectCardAction(card=card)


@pytest.mark.parametrize("deterministic", [False, True])
def test_burn_preserves_fallback_options_and_provenance(
    monkeypatch: pytest.MonkeyPatch, observation: MatchView, deterministic: bool
) -> None:
    monkeypatch.setitem(
        REGISTRY,
        "configured",
        BotSpec(
            "configured",
            ConfiguredFallback,
            deterministic,
            {"version": "test"},
            FallbackOptions,
        ),
    )
    resolved = resolve_players([
        PlayerConfig(
            bot="controlled_burn",
            options={
                "K": 0,
                "fallback_strategy": "configured",
                "fallback_options": {"prefer_high": False},
            },
        )
    ])[0]
    assert resolved.build(123).act(observation) == SelectCardAction(card=5)
    assert resolved.deterministic is deterministic
    assert resolved.recorded_options["fallback_options"] == {"prefer_high": False}
    assert resolved.metadata["fallback_metadata"]["version"] == "test"


@pytest.mark.parametrize("communication", [False, True])
def test_burn_is_reproducible_across_backends(communication: bool) -> None:
    players = [
        PlayerConfig(bot="controlled_burn", options={"K": 3, "fallback_strategy": "random"}),
        PlayerConfig(bot="closest_gap"),
    ]
    protocol = MatchProtocol(communication_enabled=communication, end_condition="fixed_hands", hands=2)
    sequential = run_arena(players, 4, 123, protocol=protocol)
    parallel = run_arena(players, 4, 123, protocol=protocol, config=RunConfig(backend="process", concurrency=2))
    assert sequential == parallel
    assert parallel.finished == 4
    assert parallel.reproducible


def test_burn_sends_custom_fallback_factory_to_process_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        REGISTRY,
        "configured",
        BotSpec(
            "configured",
            ConfiguredFallback,
            True,
            {},
            FallbackOptions,
        ),
    )
    players = [
        PlayerConfig(bot="controlled_burn", options={"K": 0, "fallback_strategy": "configured"}),
        PlayerConfig(bot="random"),
    ]
    protocol = MatchProtocol(end_condition="fixed_hands", hands=1)
    result = run_arena(players, 2, 123, protocol=protocol, config=RunConfig(backend="process"))
    assert result.finished == 2
