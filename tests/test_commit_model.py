"""Classic commit model: select commits atomically, unanimity starts resolution."""

import pytest
from pydantic import TypeAdapter

from sixnimmt.engine.actions import (
    Action,
    CommitAction,
    SelectCardAction,
    SendMessageAction,
    UncommitAction,
)
from sixnimmt.engine.errors import EngineRejection, ErrorCode
from sixnimmt.engine.events import Event
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.state import Phase
from sixnimmt.engine.transition import transition

ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)
EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


def _parse(data: dict) -> Action:
    return ACTION_ADAPTER.validate_python(data)


def _two_player_match() -> tuple:
    return create_match("m_01", ["alice", "bob"], match_seed=12345)


def test_select_card_for_unknown_player_is_rejected() -> None:
    state, _ = _two_player_match()

    with pytest.raises(EngineRejection):
        transition(state, "mallory", _parse({"type": "select_card", "card": 96}), MatchProtocol(), GameRules())


def test_select_card_must_be_in_hand() -> None:
    state, _ = _two_player_match()

    with pytest.raises(EngineRejection) as exc_info:
        transition(state, "alice", _parse({"type": "select_card", "card": 55}), MatchProtocol(), GameRules())

    assert exc_info.value.code == ErrorCode.CARD_NOT_IN_HAND


def test_select_card_commits_atomically_and_reveals_nothing() -> None:
    state, _ = _two_player_match()

    card = state.players[0].hand[0]
    new_state, events = transition(
        state, "alice", _parse({"type": "select_card", "card": card}), MatchProtocol(), GameRules()
    )

    alice = next(player for player in new_state.players if player.player_id == "alice")
    assert alice.selection == card
    assert alice.committed is True
    bob = next(player for player in new_state.players if player.player_id == "bob")
    assert bob.selection is None
    assert bob.committed is False
    assert [event.type for event in events] == [
        "action_counted",
        "selection_made",
        "selection_registered",
        "player_committed",
    ]
    selection_made = next(event for event in events if event.type == "selection_made")
    assert selection_made.audience == "player:alice"
    assert selection_made.data == {"player_id": "alice", "card": card}
    registered = next(event for event in events if event.type == "selection_registered")
    assert registered.data == {"player_id": "alice"}
    assert "card" not in registered.data
    committed = next(event for event in events if event.type == "player_committed")
    assert committed.audience == "public"
    assert "card" not in committed.data


def test_reselecting_replaces_selection_without_losing_a_card() -> None:
    state, _ = _two_player_match()
    original_hand = state.players[0].hand
    card_a, card_b = original_hand[0], original_hand[1]
    selected, _ = transition(
        state, "alice", _parse({"type": "select_card", "card": card_a}), MatchProtocol(), GameRules()
    )

    reselected, events = transition(
        selected, "alice", _parse({"type": "select_card", "card": card_b}), MatchProtocol(), GameRules()
    )

    alice = next(player for player in reselected.players if player.player_id == "alice")
    assert alice.selection == card_b
    assert alice.committed is True
    assert alice.hand == original_hand
    assert [event.type for event in events] == [
        "action_counted",
        "selection_cleared",
        "player_uncommitted",
        "selection_made",
        "selection_registered",
        "player_committed",
    ]


def test_final_commit_reveals_all_selections_in_public() -> None:
    state, _ = _two_player_match()
    alice_card = state.players[0].hand[0]
    bob_card = state.players[1].hand[0]
    after_alice, _ = transition(
        state, "alice", _parse({"type": "select_card", "card": alice_card}), MatchProtocol(), GameRules()
    )

    after_bob, events = transition(
        after_alice, "bob", _parse({"type": "select_card", "card": bob_card}), MatchProtocol(), GameRules()
    )

    assert after_bob.phase == Phase.SELECTING
    assert after_bob.resolution is None
    assert after_bob.play_number == 2
    assert after_bob.revealed_this_hand == (tuple(sorted((alice_card, bob_card))),)
    assert [event.type for event in events[:6]] == [
        "action_counted",
        "selection_made",
        "selection_registered",
        "player_committed",
        "play_committed",
        "cards_revealed",
    ]
    revealed = next(event for event in events if event.type == "cards_revealed")
    assert revealed.audience == "public"
    assert revealed.data == {"selections": {"alice": alice_card, "bob": bob_card}}
    placed = [event for event in events if event.type == "card_placed"]
    assert len(placed) == 2
    ended = [event for event in events if event.type == "play_ended"]
    assert len(ended) == 1


def test_commit_without_selection_is_rejected() -> None:
    state, _ = _two_player_match()

    with pytest.raises(EngineRejection) as exc_info:
        transition(state, "alice", CommitAction(), MatchProtocol(), GameRules())

    assert exc_info.value.code == ErrorCode.NO_SELECTION_TO_COMMIT


def test_uncommit_and_message_are_rejected_when_communication_disabled() -> None:
    state, _ = _two_player_match()
    card = state.players[0].hand[0]
    selected, _ = transition(
        state, "alice", _parse({"type": "select_card", "card": card}), MatchProtocol(), GameRules()
    )

    with pytest.raises(EngineRejection) as exc_info:
        transition(selected, "alice", UncommitAction(), MatchProtocol(), GameRules())
    assert exc_info.value.code == ErrorCode.COMMUNICATION_DISABLED

    with pytest.raises(EngineRejection) as exc_info:
        transition(
            selected,
            "alice",
            SendMessageAction(visibility="table", body="hello"),
            MatchProtocol(),
            GameRules(),
        )
    assert exc_info.value.code == ErrorCode.COMMUNICATION_DISABLED


def test_rejection_leaves_state_unchanged_and_emits_nothing() -> None:
    state, _ = _two_player_match()
    frozen = state.model_dump_json()

    with pytest.raises(EngineRejection):
        transition(state, "alice", _parse({"type": "select_card", "card": 55}), MatchProtocol(), GameRules())

    assert state.model_dump_json() == frozen


def test_actions_and_events_round_trip_through_json() -> None:
    action = SelectCardAction(card=62)
    event_data = {
        "type": "selection_made",
        "match_id": "m_01",
        "audience": "player:alice",
        "hand": 1,
        "play": 1,
        "data": {"player_id": "alice", "card": 62},
    }

    assert ACTION_ADAPTER.validate_json(ACTION_ADAPTER.dump_json(action)) == action
    assert EVENT_ADAPTER.validate_python(event_data).type == "selection_made"
