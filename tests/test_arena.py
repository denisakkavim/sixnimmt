"""Bot isolation, deterministic scheduling, and bounded in-process matches."""

import random
from dataclasses import asdict, replace

import pytest

from sixnimmt_server.arena.bots import BotObservation, RandomBot
from sixnimmt_server.arena.runner import ArenaError, derive_seed, observe_player, run_arena, run_match
from sixnimmt_server.engine.actions import ChooseRowAction, SelectCardAction
from sixnimmt_server.engine.events import Event
from sixnimmt_server.engine.setup import create_match
from sixnimmt_server.engine.state import MatchState, Phase
from sixnimmt_server.engine.views import RowView


@pytest.fixture
def observation() -> BotObservation:
    return BotObservation(
        player_id="player_1",
        decision="select_card",
        hand=(1, 20, 55),
        rows=tuple(RowView(index=index, cards=(card,)) for index, card in enumerate((10, 30, 60, 90))),
        hand_number=1,
        play_number=1,
    )


def test_random_bot_selects_only_from_own_hand(observation: BotObservation) -> None:
    bot = RandomBot(123)
    choices: set[int] = set()
    for _ in range(100):
        action = bot.act(observation)
        assert isinstance(action, SelectCardAction)
        choices.add(action.card)
    assert choices == set(observation.hand)


def test_random_bot_can_choose_every_row(observation: BotObservation) -> None:
    bot = RandomBot(123)
    choice = replace(observation, decision="choose_row")
    choices: set[int] = set()
    for _ in range(100):
        action = bot.act(choice)
        assert isinstance(action, ChooseRowAction)
        choices.add(action.row_index)
    assert choices == {0, 1, 2, 3}


def test_random_bot_is_reproducible(observation: BotObservation) -> None:
    first, second = RandomBot(9), RandomBot(9)
    assert [first.act(observation) for _ in range(100)] == [second.act(observation) for _ in range(100)]


def test_bot_draws_do_not_change_other_bots(observation: BotObservation) -> None:
    first, control, other = RandomBot(9), RandomBot(9), RandomBot(7)
    for _ in range(100):
        other.act(observation)
    assert first.act(observation) == control.act(observation)


def test_arena_does_not_touch_global_random_state() -> None:
    before = random.getstate()
    run_arena(["random", "random"], 2, 123)
    assert random.getstate() == before


def test_observation_excludes_authoritative_state() -> None:
    state, _ = create_match("private", ["player_1", "player_2"], 938458394)
    observation = observe_player(state, "player_1")
    assert set(asdict(observation)) == {"player_id", "decision", "hand", "rows", "hand_number", "play_number"}
    assert observation.hand == state.players[0].hand
    assert observation.rows == tuple(RowView(index=row.index, cards=row.cards) for row in state.rows)
    assert not hasattr(observation, "__dict__") or "match_seed" not in vars(observation)


def test_hidden_changes_leave_observation_identical() -> None:
    state, _ = create_match("private", ["player_1", "player_2"], 123)
    changed_opponent = state.players[1].model_copy(
        update={"hand": (77,), "selection": 77, "actions_taken_this_play": 90}
    )
    changed = state.model_copy(
        update={"players": (state.players[0], changed_opponent), "match_seed": 999, "undealt_remainder": ()}
    )
    assert observe_player(state, "player_1") == observe_player(changed, "player_1")


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
    isolated = run_match(bots, derive_seed(123, "match", 2), match_id="arena_2")
    assert isolated.final_state == finals[2]


class RecordingBot:
    def __init__(self, seed: int) -> None:
        self.bot = RandomBot(seed)
        self.observations: list[BotObservation] = []
        self.actions: list[SelectCardAction | ChooseRowAction] = []

    def act(self, observation: BotObservation) -> SelectCardAction | ChooseRowAction:
        self.observations.append(observation)
        action = self.bot.act(observation)
        self.actions.append(action)
        return action


class ScriptedBot:
    def __init__(self, actions: list[SelectCardAction | ChooseRowAction]) -> None:
        self.actions = iter(actions)

    def act(self, observation: BotObservation) -> SelectCardAction | ChooseRowAction:
        return next(self.actions)


def test_recorded_actions_reproduce_every_final_state_field() -> None:
    bots = [RecordingBot(3), RecordingBot(5), RecordingBot(7)]
    first = run_match(bots, 123)
    second = run_match([ScriptedBot(bot.actions) for bot in bots], 123)
    assert first == second
    assert any(observation.decision == "choose_row" for bot in bots for observation in bot.observations)
    for bot in bots:
        selections = [item for item in bot.observations if item.decision == "select_card"]
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
            assert observation.player_id == expected_player
            assert observation.decision == "choose_row"
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


def test_action_limit_stops_match_with_reproduction_context() -> None:
    with pytest.raises(ArenaError, match=r"action limit 1 exhausted.*seed 123.*hand 1.*play 1"):
        run_match([RandomBot(1), RandomBot(2)], 123, max_actions=1)


def test_invalid_bot_action_fails_without_retry() -> None:
    bot = ScriptedBot([ChooseRowAction(row_index=0)])
    with pytest.raises(ArenaError, match=r"bot decision failed.*seed 123.*player player_1"):
        run_match([bot, RandomBot(2)], 123)


def test_bot_exception_is_contextualized() -> None:
    with pytest.raises(ArenaError, match=r"bot decision failed.*seed 123") as caught:
        run_match([ScriptedBot([]), RandomBot(2)], 123)
    assert isinstance(caught.value.__cause__, StopIteration)
