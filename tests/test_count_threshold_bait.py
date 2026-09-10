"""Candidate ranking and information limits for count-threshold bait."""

import pytest
from pydantic import ValidationError

from sixnimmt.arena.bots import CountThresholdBaitBot, CountThresholdBaitOptions
from sixnimmt.arena.bots.count_threshold_bait import CandidateRanking
from sixnimmt.arena.bots.highest_card import HighestCardBot
from sixnimmt.arena.players import PlayerConfig, resolve_players
from sixnimmt.arena.runner import RunConfig, run_arena
from sixnimmt.engine.actions import ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.audience import Viewer
from sixnimmt.engine.fold import build_view
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.views import MatchView, PlayHistoryView, RevealedCardView, RowView, ViewRole


@pytest.fixture
def observation() -> MatchView:
    _, events = create_match("bait", ["a", "b"], 123)
    view = build_view(events, Viewer(ViewRole.PLAYER, "a"))
    rows = ((3, 6, 9, 12, 14), (21, 22, 25, 28, 30), (60,), (90,))
    return view.model_copy(
        update={
            "rows": tuple(RowView(index=index, cards=cards) for index, cards in enumerate(rows)),
            "you": view.you.model_copy(update={"hand": (19, 34, 36, 95)}),
        }
    )


@pytest.mark.parametrize(
    "ranking, revealed, expected",
    [
        ("most_intervening", (), 19),
        ("most_intervening", ((16,),), 36),
        ("cheapest_pickup", ((16,),), 19),
        ("highest_card", (), 36),
    ],
)
def test_ranking_options_choose_different_candidates(
    observation: MatchView, ranking: CandidateRanking, revealed: tuple[tuple[int, ...], ...], expected: int
) -> None:
    view = observation.model_copy(update={"revealed_this_hand": revealed})
    assert CountThresholdBaitBot(3, ranking, HighestCardBot()).act(view) == SelectCardAction(card=expected)


@pytest.mark.parametrize("threshold, expected", [(4, 19), (5, 95)])
def test_threshold_is_inclusive(observation: MatchView, threshold: int, expected: int) -> None:
    assert CountThresholdBaitBot(threshold, "most_intervening", HighestCardBot()).act(observation) == SelectCardAction(
        card=expected
    )


def test_own_cards_cannot_count_as_possible_opponent_cards(observation: MatchView) -> None:
    view = observation.model_copy(update={"you": observation.you.model_copy(update={"hand": (34, 35, 36, 95)})})
    assert CountThresholdBaitBot(4, "most_intervening", HighestCardBot()).act(view) == SelectCardAction(card=95)


def test_board_cards_cannot_count_as_possible_opponent_cards(observation: MatchView) -> None:
    rows = (*observation.rows[:2], RowView(index=2, cards=(16, 60)), observation.rows[3])
    view = observation.model_copy(update={"rows": rows})
    assert CountThresholdBaitBot(4, "most_intervening", HighestCardBot()).act(view) == SelectCardAction(card=36)


def test_card_knowledge_resets_at_each_deal(observation: MatchView) -> None:
    bot = CountThresholdBaitBot(4, "most_intervening", HighestCardBot())
    first_hand = observation.model_copy(update={"revealed_this_hand": ((16,),)})
    assert bot.act(first_hand) == SelectCardAction(card=36)
    next_hand = observation.model_copy(
        update={
            "hand_number": 2,
            "play_history": (
                PlayHistoryView(hand_number=1, play_number=1, cards=(RevealedCardView(player_id="b", card=16),)),
            ),
        }
    )
    assert bot.act(next_hand) == SelectCardAction(card=19)


@pytest.mark.parametrize("own_pile", [False, True])
def test_captured_row_start_cards_are_unavailable(observation: MatchView, own_pile: bool) -> None:
    if own_pile:
        view = observation.model_copy(update={"you": observation.you.model_copy(update={"penalty_cards": (16,)})})
    else:
        players = (observation.players[0].model_copy(update={"penalty_cards": (16,)}), *observation.players[1:])
        view = observation.model_copy(update={"players": players})
    assert CountThresholdBaitBot(4, "most_intervening", HighestCardBot()).act(view) == SelectCardAction(card=36)


def test_only_the_applicable_row_can_qualify_for_bait(observation: MatchView) -> None:
    view = observation.model_copy(update={"you": observation.you.model_copy(update={"hand": (1, 65, 95)})})
    assert CountThresholdBaitBot(1, "most_intervening", HighestCardBot()).act(view) == SelectCardAction(card=95)


@pytest.mark.parametrize("ranking", ["most_intervening", "cheapest_pickup"])
def test_ranking_ties_choose_lowest_card(observation: MatchView, ranking: CandidateRanking) -> None:
    view = observation.model_copy(update={"you": observation.you.model_copy(update={"hand": (20, 19)})})
    assert CountThresholdBaitBot(3, ranking, HighestCardBot()).act(view) == SelectCardAction(card=19)


def test_row_choice_uses_current_cheapest_row(observation: MatchView) -> None:
    view = observation.model_copy(update={"legal_actions": ("choose_row",)})
    assert CountThresholdBaitBot(3, "highest_card", HighestCardBot()).act(view) == ChooseRowAction(row_index=2)


def test_selected_card_is_committed(observation: MatchView) -> None:
    view = observation.model_copy(update={"legal_actions": ("select_card", "commit")})
    assert CountThresholdBaitBot(3, "highest_card", HighestCardBot()).act(view) == CommitAction()


@pytest.mark.parametrize("field", ["intervening_card_threshold", "candidate_ranking", "fallback_strategy"])
def test_configuration_has_no_default_policy(field: str) -> None:
    options = {"intervening_card_threshold": 3, "candidate_ranking": "most_intervening", "fallback_strategy": "random"}
    del options[field]
    with pytest.raises(ValidationError):
        CountThresholdBaitOptions.model_validate(options)


@pytest.mark.parametrize(
    "changes",
    [
        {"intervening_card_threshold": 0},
        {"intervening_card_threshold": True},
        {"candidate_ranking": "unknown"},
        {"fallback_strategy": "unknown"},
        {"fallback_options": {"typo": 1}},
    ],
)
def test_invalid_options_are_rejected_before_running(changes: dict) -> None:
    options = {
        "intervening_card_threshold": 3,
        "candidate_ranking": "most_intervening",
        "fallback_strategy": "random",
        **changes,
    }
    with pytest.raises(ValueError):
        resolve_players([PlayerConfig(bot="count_threshold_bait", options=options)])


@pytest.mark.parametrize("communication", [False, True])
def test_bait_with_nested_fallback_is_reproducible_across_backends(communication: bool) -> None:
    players = [
        PlayerConfig(
            bot="count_threshold_bait",
            options={
                "intervening_card_threshold": 3,
                "candidate_ranking": "most_intervening",
                "fallback_strategy": "controlled_burn",
                "fallback_options": {"K": 3, "fallback_strategy": "random"},
            },
        ),
        PlayerConfig(bot="closest_gap"),
    ]
    protocol = MatchProtocol(communication_enabled=communication, end_condition="fixed_hands", hands=2)
    sequential = run_arena(players, 4, 123, protocol=protocol)
    parallel = run_arena(players, 4, 123, protocol=protocol, config=RunConfig(backend="process", concurrency=2))
    assert sequential == parallel
    assert parallel.finished == 4
    assert parallel.reproducible
