"""Observable card policies and shared baseline contracts."""

import pytest

from sixnimmt.arena.bots import REGISTRY
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.arena.runner import run_arena
from sixnimmt.engine.actions import ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.audience import Viewer
from sixnimmt.engine.fold import build_view
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.views import MatchView, RowView, ViewRole

BASELINE_NAMES = (
    "random",
    "lowest_card",
    "highest_card",
    "lowest_fitting_card",
    "highest_fitting_card",
    "closest_gap",
    "coldest_row",
    "hand_flexibility",
)


@pytest.fixture
def observation() -> MatchView:
    _, events = create_match("baselines", ["a", "b"], 123)
    return build_view(events, Viewer(ViewRole.PLAYER, "a"))


@pytest.mark.parametrize(
    ("strategy", "expected_card"),
    [
        ("lowest_card", 1),
        ("highest_card", 100),
        ("lowest_fitting_card", 19),
        ("highest_fitting_card", 61),
        ("closest_gap", 41),
        ("coldest_row", 61),
    ],
)
def test_card_policies_make_distinct_choices(observation: MatchView, strategy: str, expected_card: int) -> None:
    rows = tuple(
        RowView(index=index, cards=cards)
        for index, cards in enumerate([(9, 10), (30, 40), (60,), (70, 80, 90, 95, 99)])
    )
    view = observation.model_copy(
        update={"rows": rows, "you": observation.you.model_copy(update={"hand": (100, 61, 41, 19, 1)})}
    )
    assert REGISTRY[strategy].build(123).act(view) == SelectCardAction(card=expected_card)


@pytest.mark.parametrize("strategy", ["closest_gap", "coldest_row"])
def test_fitting_ties_use_lowest_card(observation: MatchView, strategy: str) -> None:
    rows = tuple(RowView(index=index, cards=(card,)) for index, card in enumerate((10, 20, 30, 40)))
    view = observation.model_copy(
        update={"rows": rows, "you": observation.you.model_copy(update={"hand": (41, 31, 21, 11)})}
    )
    assert REGISTRY[strategy].build(123).act(view) == SelectCardAction(card=11)


def test_coldest_row_occupancy_ties_ignore_gap_and_penalties(observation: MatchView) -> None:
    rows = tuple(RowView(index=index, cards=(card,)) for index, card in enumerate((11, 30, 60, 90)))
    view = observation.model_copy(
        update={"rows": rows, "you": observation.you.model_copy(update={"hand": (29, 31, 61, 91)})}
    )
    assert REGISTRY["coldest_row"].build(123).act(view) == SelectCardAction(card=29)


@pytest.mark.parametrize("strategy", ["lowest_fitting_card", "highest_fitting_card", "closest_gap", "coldest_row"])
@pytest.mark.parametrize(
    ("hand", "expected_card"),
    [((1, 25, 61), 61), ((1, 25), 1), ((26, 25), 25)],
    ids=["cannot-skip-full-applicable-row", "cheapest-pickup", "fallback-ties-use-lowest-card"],
)
def test_fitting_policies_share_eligibility_and_fallback(
    observation: MatchView, strategy: str, hand: tuple[int, ...], expected_card: int
) -> None:
    rows = tuple(
        RowView(index=index, cards=cards) for index, cards in enumerate([(10,), (20, 21, 22, 23, 24), (60,), (90,)])
    )
    view = observation.model_copy(update={"rows": rows, "you": observation.you.model_copy(update={"hand": hand})})
    assert REGISTRY[strategy].build(123).act(view) == SelectCardAction(card=expected_card)


@pytest.mark.parametrize(
    ("row_cards", "expected_card"),
    [
        ([(1,), (2,), (3,), (4,)], 40),
        ([(1,), (31, 32, 33, 34, 35), (55,), (75,)], 60),
        ([(90,), (91,), (92,), (93,)], 40),
    ],
    ids=["retain-distributed-coverage-and-break-tie-by-card", "cost-before-coverage", "coverage-among-pickups"],
)
def test_hand_flexibility_ranks_cost_then_coverage(
    observation: MatchView, row_cards: list[tuple[int, ...]], expected_card: int
) -> None:
    rows = tuple(RowView(index=index, cards=cards) for index, cards in enumerate(row_cards))
    view = observation.model_copy(
        update={"rows": rows, "you": observation.you.model_copy(update={"hand": (80, 60, 40, 20)})}
    )
    assert REGISTRY["hand_flexibility"].build(123).act(view) == SelectCardAction(card=expected_card)


@pytest.mark.parametrize("strategy", BASELINE_NAMES)
def test_baseline_plays_final_card(observation: MatchView, strategy: str) -> None:
    view = observation.model_copy(update={"you": observation.you.model_copy(update={"hand": (1,)})})
    assert REGISTRY[strategy].build(123).act(view) == SelectCardAction(card=1)


@pytest.mark.parametrize("strategy", BASELINE_NAMES)
def test_baseline_takes_cheapest_row_with_lowest_index(observation: MatchView, strategy: str) -> None:
    rows = (
        RowView(index=3, cards=(10,)),
        RowView(index=2, cards=(55,)),
        RowView(index=1, cards=(1, 2)),
        RowView(index=0, cards=(11,)),
    )
    view = observation.model_copy(update={"rows": rows, "legal_actions": ("choose_row",)})
    assert REGISTRY[strategy].build(123).act(view) == ChooseRowAction(row_index=1)


@pytest.mark.parametrize("strategy", BASELINE_NAMES)
def test_baseline_commits_selected_card(observation: MatchView, strategy: str) -> None:
    view = observation.model_copy(update={"legal_actions": ("select_card", "commit")})
    assert REGISTRY[strategy].build(123).act(view) == CommitAction()


@pytest.mark.parametrize("communication", [False, True])
def test_mixed_baseline_matches_are_reproducible(communication: bool) -> None:
    players = [PlayerConfig(bot=name) for name in BASELINE_NAMES]
    protocol = MatchProtocol(communication_enabled=communication, end_condition="fixed_hands", hands=2)
    first = run_arena(players, games=3, seed=123, protocol=protocol)
    second = run_arena(players, games=3, seed=123, protocol=protocol)
    assert first == second
    assert first.finished == 3
    assert first.reproducible
