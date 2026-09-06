"""Match creation rejects malformed line-ups, and sequencing numbers a batch."""

import pytest

from sixnimmt.engine.errors import EngineRejection, ErrorCode
from sixnimmt.engine.events import assign_sequence
from sixnimmt.engine.rules import GameRules
from sixnimmt.engine.setup import create_match, open_match, start_hand, start_match
from sixnimmt.engine.state import MatchState, Phase, PlayerSeat


@pytest.mark.parametrize("count", [0, 1, 11, 20])
def test_player_counts_outside_the_rules_are_rejected(count: int) -> None:
    with pytest.raises(EngineRejection) as caught:
        open_match("m_01", [f"p{seat}" for seat in range(count)], match_seed=1)

    assert caught.value.code == ErrorCode.INVALID_PLAYER_COUNT


def test_narrowed_rules_reject_a_line_up_the_default_rules_would_allow() -> None:
    with pytest.raises(EngineRejection) as caught:
        open_match("m_01", ["a", "b", "c"], match_seed=1, rules=GameRules(max_players=2))

    assert caught.value.code == ErrorCode.INVALID_PLAYER_COUNT


def test_duplicate_player_ids_are_rejected_and_named() -> None:
    with pytest.raises(EngineRejection) as caught:
        open_match("m_01", ["alice", "bob", "alice"], match_seed=1)

    assert caught.value.code == ErrorCode.DUPLICATE_PLAYER_ID
    assert "alice" in str(caught.value)


def test_empty_player_ids_are_rejected() -> None:
    with pytest.raises(EngineRejection) as caught:
        open_match("m_01", ["alice", ""], match_seed=1)

    assert caught.value.code == ErrorCode.INVALID_PLAYER_ID


def test_seats_carry_display_names_and_agent_metadata_into_state() -> None:
    seats = [
        PlayerSeat(player_id="alice", display_name="Player 1", agent_metadata={"model": "claude-opus-5"}),
        PlayerSeat(player_id="bob", display_name="Player 2"),
    ]

    state, events = open_match("m_01", seats, match_seed=1)

    assert state.players[0].display_name == "Player 1"
    assert state.players[0].agent_metadata == {"model": "claude-opus-5"}
    public = next(event for event in events if event.type == "match_created" and event.audience == "public")
    admin = next(event for event in events if event.type == "match_created" and event.audience == "admin")
    assert all("agent_metadata" not in entry for entry in public.data["players"])
    assert admin.data["players"][0]["agent_metadata"] == {"model": "claude-opus-5"}


def test_a_player_without_a_display_name_is_shown_by_id() -> None:
    _, events = open_match("m_01", ["alice", "bob"], match_seed=1)

    public = next(event for event in events if event.type == "match_created" and event.audience == "public")

    assert [entry["display_name"] for entry in public.data["players"]] == ["alice", "bob"]


def test_starting_an_already_started_match_is_rejected() -> None:
    started, _ = create_match("m_01", ["alice", "bob"], match_seed=1)

    with pytest.raises(EngineRejection) as caught:
        start_match(started)

    assert caught.value.code == ErrorCode.WRONG_PHASE


def test_opened_match_waits_in_setup_until_started() -> None:
    opened, _ = open_match("m_01", ["alice", "bob"], match_seed=1)

    assert opened.phase == Phase.SETUP
    assert all(player.hand == () for player in opened.players)
    assert opened.rows == ()

    started, _ = start_match(opened)

    assert started.phase == Phase.SELECTING
    assert all(len(player.hand) == 10 for player in started.players)


def test_dealing_without_a_match_seed_fails_loudly() -> None:
    seedless = MatchState(match_id="m_01", phase=Phase.SETUP)

    with pytest.raises(ValueError, match="without a match seed"):
        start_hand(seedless, hand_number=2)


def test_assign_sequence_numbers_a_batch_contiguously() -> None:
    _, events = open_match("m_01", ["alice", "bob"], match_seed=1)

    numbered = assign_sequence(events, first_seq=7, server_action_seq=3)

    assert [event.seq for event in numbered] == [7, 8, 9]
    assert all(event.server_action_seq == 3 for event in numbered)
    assert [event.type for event in numbered] == [event.type for event in events]
    assert all(event.seq == 0 for event in events), "numbering must not mutate the batch"


@pytest.mark.parametrize(
    ("player_id", "description"),
    [
        ("alice:bot", "a colon the event audience cannot carry"),
        ("\ud800", "a lone surrogate that cannot be encoded as UTF-8"),
        ("alice bob", "a space"),
        ("a" * 65, "longer than the grammar allows"),
        ("", "empty"),
    ],
)
def test_a_player_id_outside_the_wire_safe_grammar_is_rejected(player_id: str, description: str) -> None:
    """An id must survive an audience, a token map key and a UTF-8 log."""
    with pytest.raises(EngineRejection) as caught:
        open_match("m_01", [player_id, "bob"], match_seed=1)

    assert caught.value.code == ErrorCode.INVALID_PLAYER_ID
