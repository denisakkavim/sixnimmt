"""Bot isolation, deterministic scheduling, and bounded in-process matches."""

import random

import pytest

from sixnimmt_server.arena.bots import REGISTRY, RandomBot, Rejection
from sixnimmt_server.arena.runner import MatchOutcome, derive_seed, run_arena, run_match
from sixnimmt_server.engine.actions import Action, ChooseRowAction, SelectCardAction
from sixnimmt_server.engine.audience import Viewer
from sixnimmt_server.engine.events import Event
from sixnimmt_server.engine.fold import build_view
from sixnimmt_server.engine.setup import create_match
from sixnimmt_server.engine.state import MatchState, Phase, PlayerSeat
from sixnimmt_server.engine.views import MatchView, ViewRole


@pytest.fixture
def observation() -> MatchView:
    _, events = create_match("test", ["player_1", "player_2"], 123)
    return build_view(events, Viewer(ViewRole.PLAYER, "player_1"))


def test_random_bot_selects_only_from_own_hand(observation: MatchView) -> None:
    bot = RandomBot(123)
    choices: set[int] = set()
    for _ in range(100):
        action = bot.act(observation)
        assert isinstance(action, SelectCardAction)
        choices.add(action.card)
    assert choices == set(observation.you.hand)


def test_random_bot_can_choose_every_row(observation: MatchView) -> None:
    bot = RandomBot(123)
    choice = observation.model_copy(update={"legal_actions": ("choose_row",), "phase": Phase.AWAITING_ROW_CHOICE})
    choices: set[int] = set()
    for _ in range(100):
        action = bot.act(choice)
        assert isinstance(action, ChooseRowAction)
        choices.add(action.row_index)
    assert choices == {0, 1, 2, 3}


def test_random_bot_is_reproducible(observation: MatchView) -> None:
    first, second = RandomBot(9), RandomBot(9)
    assert [first.act(observation) for _ in range(100)] == [second.act(observation) for _ in range(100)]


def test_bot_draws_do_not_change_other_bots(observation: MatchView) -> None:
    first, control, other = RandomBot(9), RandomBot(9), RandomBot(7)
    for _ in range(100):
        other.act(observation)
    assert first.act(observation) == control.act(observation)


def test_arena_does_not_touch_global_random_state() -> None:
    before = random.getstate()
    run_arena(["random", "random"], 2, 123)
    assert random.getstate() == before


def test_same_arena_seed_reproduces_results() -> None:
    assert run_arena(["random"] * 3, 4, 123) == run_arena(["random"] * 3, 4, 123)


def test_seed_streams_are_distinct_and_stable() -> None:
    seeds = [derive_seed(123, "match", game) for game in range(10)]
    seeds.extend(derive_seed(123, "bot", game, seat) for game in range(10) for seat in range(10))
    assert len(set(seeds)) == len(seeds)
    assert derive_seed(123, "match", 0) == 8770460399439567526


def test_later_game_can_be_reproduced_without_earlier_games() -> None:
    finals: list[MatchState] = []

    def record(state: MatchState, events: tuple[Event, ...]) -> None:
        if state.phase == Phase.FINISHED:
            finals.append(state)

    run_arena(["random"] * 2, 3, 123, observer=record)
    bots = [RandomBot(derive_seed(123, "bot", 2, seat)) for seat in range(2)]
    seats = [
        PlayerSeat(
            player_id=f"player_{seat + 1}",
            display_name=f"Player {seat + 1}",
            agent_metadata=REGISTRY["random"].metadata,
        )
        for seat in range(2)
    ]
    isolated = run_match(bots, derive_seed(123, "match", 2), match_id="arena_2", seats=seats)
    assert isolated.final_state == finals[2]


class RecordingBot:
    def __init__(self, seed: int) -> None:
        self.bot = RandomBot(seed)
        self.observations: list[MatchView] = []
        self.actions: list[Action] = []

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        self.observations.append(view)
        action = self.bot.act(view, rejection)
        self.actions.append(action)
        return action


class ScriptedBot:
    def __init__(self, actions: list[Action]) -> None:
        self.actions = iter(actions)

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        return next(self.actions)


def test_recorded_actions_reproduce_every_final_state_field() -> None:
    bots = [RecordingBot(3), RecordingBot(5), RecordingBot(7)]
    first = run_match(bots, 123)
    second = run_match([ScriptedBot(bot.actions) for bot in bots], 123)
    assert first == second
    assert any(observation.phase == Phase.AWAITING_ROW_CHOICE for bot in bots for observation in bot.observations)
    for bot in bots:
        selections = [item for item in bot.observations if item.phase == Phase.SELECTING]
        assert len(selections) == first.final_state.hand_number * 10
        assert [(item.hand_number, item.play_number) for item in selections] == [
            (hand, play) for hand in range(1, first.final_state.hand_number + 1) for play in range(1, 11)
        ]


def test_observer_sees_setup_and_all_transitions_without_changing_results() -> None:
    batches: list[tuple[Event, ...]] = []

    def record(state: MatchState, events: tuple[Event, ...]) -> None:
        batches.append(events)

    first = run_match([RandomBot(3), RandomBot(5)], 123, observer=record)
    second = run_match([RandomBot(3), RandomBot(5)], 123)
    assert first == second
    assert len(batches) == first.actions + 1
    assert batches[0][0].type == "match_created"
    assert batches[-1][-1].type == "match_ended"


def test_same_seeds_produce_identical_events_except_timestamps() -> None:
    stream: list[dict] = []

    def record(state: MatchState, events: tuple[Event, ...]) -> None:
        stream.extend(event.model_dump(exclude={"timestamp"}) for event in events)

    run_match([RandomBot(3), RandomBot(5)], 123, observer=record)
    first = list(stream)
    stream.clear()
    run_match([RandomBot(3), RandomBot(5)], 123, observer=record)
    assert first == stream


def test_only_awaited_player_is_scheduled_for_row_choice() -> None:
    bots = [RecordingBot(3), RecordingBot(5)]
    expected_player: str | None = None
    decisions_checked = 0

    def inspect(state: MatchState, events: tuple[Event, ...]) -> None:
        nonlocal expected_player, decisions_checked
        if expected_player is not None:
            seat = int(expected_player.split("_")[1]) - 1
            observation = bots[seat].observations[-1]
            assert observation.you.player_id == expected_player
            assert observation.phase == Phase.AWAITING_ROW_CHOICE
            assert any(event.type == "row_choice_made" for event in events)
            decisions_checked += 1
        expected_player = state.resolution.awaiting_player if state.resolution is not None else None

    run_match(bots, 123, observer=inspect)
    assert decisions_checked > 0


def test_arena_counts_sole_wins_and_shared_wins_separately() -> None:
    result = run_arena(["random", "random"], 10, 1234)
    assert [player.wins for player in result.players] == [3, 6]
    assert [player.ties for player in result.players] == [1, 1]
    assert [player.total_score for player in result.players] == [689, 594]


@pytest.mark.parametrize("count", [0, 1, 11])
def test_rejects_invalid_player_counts(count: int) -> None:
    with pytest.raises(ValueError, match="between 2 and 10"):
        run_arena(["random"] * count, 1, 123)
    with pytest.raises(ValueError, match="between 2 and 10"):
        run_match([RandomBot(1) for _ in range(count)], 123)


@pytest.mark.parametrize("games", [0, -1])
def test_rejects_nonpositive_games(games: int) -> None:
    with pytest.raises(ValueError, match="games must be positive"):
        run_arena(["random"] * 2, games, 123)


def test_rejects_unknown_bot_before_starting() -> None:
    with pytest.raises(ValueError, match="unknown bot"):
        run_arena(["random", "unknown"], 1, 123)


@pytest.mark.parametrize("limit", [0, -1])
def test_rejects_nonpositive_action_limit(limit: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        run_match([RandomBot(1), RandomBot(2)], 123, max_actions=limit)
    with pytest.raises(ValueError, match="positive"):
        run_arena(["random"] * 2, 1, 123, max_actions_per_match=limit)


def test_action_limit_abandons_match() -> None:
    result = run_match([RandomBot(1), RandomBot(2)], 123, max_actions=1)
    assert result.outcome == MatchOutcome.ABANDONED
    assert result.reason == "match_action_limit"
    assert result.winners == ()


def test_bot_exception_is_recorded() -> None:
    result = run_match([ScriptedBot([]), RandomBot(2)], 123)
    assert result.outcome == MatchOutcome.FAILED
    assert result.ended_by == "player_1"
    assert result.reason == "StopIteration()"
