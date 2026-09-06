"""Card selection, explicit commitment, and action budgets with communication enabled."""

import pytest
from pydantic import TypeAdapter

from sixnimmt.engine.actions import Action, CommitAction, UncommitAction
from sixnimmt.engine.errors import EngineRejection, ErrorCode
from sixnimmt.engine.events import EventType
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.state import MatchState, Phase, PlayerState, ResolutionState, RowState
from sixnimmt.engine.transition import transition

_ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)
_COMMUNICATION = MatchProtocol(communication_enabled=True)


def _select(card: int) -> Action:
    return _ACTION_ADAPTER.validate_python({"type": "select_card", "card": card})


def test_communication_selection_stays_uncommitted_and_in_hand() -> None:
    state, _ = create_match("m_01", ["alice", "bob"], match_seed=12345)
    card = state.players[0].hand[0]

    selected, events = transition(state, "alice", _select(card), _COMMUNICATION, GameRules())

    alice = selected.players[0]
    assert alice.selection == card
    assert alice.committed is False
    assert card in alice.hand
    assert alice.actions_taken_this_play == 1
    assert [event.type for event in events] == ["action_counted", "selection_made", "selection_registered"]


def test_reselection_clears_only_the_callers_commitment() -> None:
    state, _ = create_match("m_01", ["alice", "bob"], match_seed=12345)
    alice_card, alice_replacement = state.players[0].hand[:2]
    bob_card = state.players[1].hand[0]
    prepared = state.model_copy(
        update={
            "players": (
                state.players[0].model_copy(update={"selection": alice_card, "committed": True}),
                state.players[1].model_copy(update={"selection": bob_card, "committed": True}),
            )
        }
    )

    selected, events = transition(
        prepared,
        "alice",
        _select(alice_replacement),
        _COMMUNICATION,
        GameRules(),
    )

    assert selected.players[0].selection == alice_replacement
    assert selected.players[0].committed is False
    assert selected.players[1].selection == bob_card
    assert selected.players[1].committed is True
    assert [event.type for event in events] == [
        "action_counted",
        "selection_cleared",
        "player_uncommitted",
        "selection_made",
        "selection_registered",
    ]


def test_final_explicit_commit_emits_commitment_before_reveal() -> None:
    state, _ = create_match("m_01", ["alice", "bob"], match_seed=12345)
    alice_card = state.players[0].hand[0]
    bob_card = state.players[1].hand[0]
    state, _ = transition(state, "alice", _select(alice_card), _COMMUNICATION, GameRules())
    state, _ = transition(state, "bob", _select(bob_card), _COMMUNICATION, GameRules())
    state, _ = transition(state, "alice", CommitAction(), _COMMUNICATION, GameRules())

    committed, events = transition(state, "bob", CommitAction(), _COMMUNICATION, GameRules())

    event_types = [event.type for event in events]
    assert event_types[:4] == ["action_counted", "player_committed", "play_committed", "cards_revealed"]
    assert event_types.count(EventType.PLAY_COMMITTED) == 1
    assert committed.play_number == 2
    assert alice_card not in committed.players[0].hand
    assert bob_card not in committed.players[1].hand


def test_uncommit_clears_selection_while_another_player_is_uncommitted() -> None:
    state, _ = create_match("m_01", ["alice", "bob"], match_seed=12345)
    card = state.players[0].hand[0]
    selected, _ = transition(state, "alice", _select(card), _COMMUNICATION, GameRules())
    committed, _ = transition(selected, "alice", CommitAction(), _COMMUNICATION, GameRules())

    uncommitted, events = transition(committed, "alice", UncommitAction(), _COMMUNICATION, GameRules())

    alice = uncommitted.players[0]
    assert alice.selection is None
    assert alice.committed is False
    assert card in alice.hand
    assert alice.actions_taken_this_play == 3
    assert [event.type for event in events] == ["action_counted", "player_uncommitted", "selection_cleared"]


def test_uncommit_rejects_an_all_committed_selecting_state() -> None:
    state, _ = create_match("m_01", ["alice", "bob"], match_seed=12345)
    prepared = state.model_copy(
        update={
            "players": tuple(
                player.model_copy(update={"selection": player.hand[0], "committed": True}) for player in state.players
            )
        }
    )

    with pytest.raises(EngineRejection) as exc_info:
        transition(prepared, "alice", UncommitAction(), _COMMUNICATION, GameRules())

    assert exc_info.value.code == ErrorCode.CANNOT_UNCOMMIT_WHEN_ALL_COMMITTED


def test_action_budget_rejects_the_next_selection_without_mutating_state() -> None:
    state, _ = create_match("m_01", ["alice", "bob"], match_seed=12345)
    first_card, second_card = state.players[0].hand[:2]
    protocol = MatchProtocol(communication_enabled=True, max_actions_per_play=1)
    selected, _ = transition(state, "alice", _select(first_card), protocol, GameRules())
    frozen = selected.model_dump_json()

    with pytest.raises(EngineRejection) as exc_info:
        transition(selected, "alice", _select(second_card), protocol, GameRules())

    assert exc_info.value.code == ErrorCode.ACTION_BUDGET_EXHAUSTED
    assert selected.model_dump_json() == frozen


def test_unlimited_action_budget_allows_repeated_reselection() -> None:
    state, _ = create_match("m_01", ["alice", "bob"], match_seed=12345)
    protocol = MatchProtocol(communication_enabled=True)
    current = state

    for card in state.players[0].hand[:4]:
        current, _ = transition(current, "alice", _select(card), protocol, GameRules())

    assert current.players[0].actions_taken_this_play == 4


def test_action_budget_resets_when_the_next_play_starts() -> None:
    state, _ = create_match("m_01", ["alice", "bob"], match_seed=12345)
    protocol = MatchProtocol(max_actions_per_play=1)
    state, _ = transition(state, "alice", _select(state.players[0].hand[0]), protocol, GameRules())

    next_play, _ = transition(state, "bob", _select(state.players[1].hand[0]), protocol, GameRules())

    assert next_play.play_number == 2
    assert all(player.actions_taken_this_play == 0 for player in next_play.players)


def test_required_row_choice_is_exempt_from_the_action_budget() -> None:
    state = MatchState(
        match_id="m_01",
        phase=Phase.AWAITING_ROW_CHOICE,
        match_seed=1,
        players=(PlayerState(player_id="bob", actions_taken_this_play=1),),
        rows=tuple(RowState(index=index, cards=(card,)) for index, card in enumerate((7, 44, 52, 88))),
        resolution=ResolutionState(
            ordered_cards=((3, "bob"),),
            awaiting_player="bob",
            scores_before_play=(("bob", 0),),
        ),
    )
    choose = _ACTION_ADAPTER.validate_python({"type": "choose_row", "row_index": 0})
    protocol = MatchProtocol(max_actions_per_play=1)

    resolved, events = transition(state, "bob", choose, protocol, GameRules())

    assert resolved.phase == Phase.SELECTING
    assert resolved.hand_number == 2
    assert resolved.play_number == 1
    assert [event.type for event in events[:3]] == ["row_choice_made", "row_taken", "card_placed"]


def test_reselecting_the_same_card_is_publicly_indistinguishable_from_a_change() -> None:
    state, _ = create_match("m_01", ["alice", "bob"], match_seed=12345)
    original, replacement = state.players[0].hand[:2]
    selected, _ = transition(state, "alice", _select(original), _COMMUNICATION, GameRules())

    _, repeat_events = transition(selected, "alice", _select(original), _COMMUNICATION, GameRules())
    _, change_events = transition(selected, "alice", _select(replacement), _COMMUNICATION, GameRules())

    public_repeat = [event.type for event in repeat_events if event.audience == "public"]
    public_change = [event.type for event in change_events if event.audience == "public"]
    assert public_repeat == public_change
    assert public_repeat == [EventType.SELECTION_CLEARED, EventType.SELECTION_REGISTERED]
