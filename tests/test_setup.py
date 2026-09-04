"""Match setup and hand dealing orchestration."""

from sixnimmt_server.engine.setup import create_match, start_hand
from sixnimmt_server.engine.state import Phase, PlayerState


def test_create_match_deals_first_hand_in_selecting() -> None:
    state, _ = create_match("m_01", ["alice", "bob"], match_seed=12345)

    assert state.match_id == "m_01"
    assert state.phase == Phase.SELECTING
    assert state.hand_number == 1
    assert state.play_number == 1
    assert state.match_seed == 12345
    assert [player.player_id for player in state.players] == ["alice", "bob"]
    assert all(len(player.hand) == 10 for player in state.players)
    assert len(state.rows) == 4
    assert len(state.undealt_remainder) == 80


def test_setup_emits_hand_started_dealt_rows_and_play_started() -> None:
    _, events = create_match("m_01", ["alice", "bob"], match_seed=12345)

    types = [event.type for event in events]
    assert types[0] == "hand_started"
    assert types[1] == "cards_dealt"
    assert types[2] == "cards_dealt"
    assert types[3] == "rows_initialised"
    assert types[4] == "play_started"


def test_cards_dealt_events_carry_each_hand_privately() -> None:
    state, events = create_match("m_01", ["alice", "bob"], match_seed=12345)

    dealt = [event for event in events if event.type == "cards_dealt"]
    assert [event.audience for event in dealt] == ["player:alice", "player:bob"]
    assert dealt[0].data["hand"] == list(state.players[0].hand)
    assert dealt[1].data["hand"] == list(state.players[1].hand)


def test_hands_follow_seed_vectors_in_deal_order() -> None:
    state, _ = create_match("m_01", ["a", "b"], match_seed=12345)

    assert list(state.players[0].hand) == [96, 36, 10, 72, 100, 32, 45, 1, 65, 94]
    assert list(state.players[1].hand) == [64, 59, 5, 27, 35, 97, 99, 43, 19, 7]
    assert [row.cards for row in state.rows] == [(9,), (28,), (2,), (81,)]


def test_seating_order_determines_deal() -> None:
    first, _ = create_match("m_01", ["alice", "bob"], match_seed=12345)
    swapped, _ = create_match("m_01", ["bob", "alice"], match_seed=12345)

    assert list(first.players[0].hand) == list(swapped.players[0].hand)
    assert list(first.players[1].hand) == list(swapped.players[1].hand)
    assert list(first.players[0].hand) != list(swapped.players[1].hand)


def test_start_hand_deals_next_hand_with_independent_seed() -> None:
    state, _ = create_match("m_01", ["a", "b"], match_seed=12345)
    state = state.model_copy(
        update={
            "players": tuple(
                PlayerState(
                    player_id=player.player_id,
                    hand=player.hand,
                    selection=player.hand[0],
                    committed=True,
                    score_this_hand=10,
                    total_score=20,
                    actions_taken_this_play=5,
                )
                for player in state.players
            ),
            "revealed_this_hand": ((1, 2),),
        }
    )

    next_state, events = start_hand(state, hand_number=3)

    assert next_state.hand_number == 3
    assert next_state.play_number == 1
    assert next_state.phase == Phase.SELECTING
    assert next_state.revealed_this_hand == ()
    assert all(player.selection is None for player in next_state.players)
    assert all(player.committed is False for player in next_state.players)
    assert all(player.score_this_hand == 0 for player in next_state.players)
    assert all(player.total_score == 20 for player in next_state.players)
    assert all(player.actions_taken_this_play == 0 for player in next_state.players)
    assert next(event.type for event in events) == "hand_started"
    third, _ = create_match("m_99", ["a", "b"], match_seed=12345)
    third_hand3, _ = start_hand(third, hand_number=3)
    assert next_state.players[0].hand == third_hand3.players[0].hand
