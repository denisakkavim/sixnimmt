"""Match and play state shapes."""

import pytest
from pydantic import ValidationError

from sixnimmt_server.engine.state import (
    MatchState,
    Phase,
    PlayerState,
    ResolutionState,
    RowState,
)


def test_phases_cover_the_full_match_lifecycle() -> None:
    assert [phase.value for phase in Phase] == [
        "setup",
        "selecting",
        "resolving",
        "awaiting_row_choice",
        "finished",
    ]


def test_player_state_defaults_to_uncommitted_with_no_selection() -> None:
    player = PlayerState(player_id="alice", hand=(4, 19, 62))

    assert player.selection is None
    assert player.committed is False
    assert player.penalty_cards == ()
    assert player.total_score == 0
    assert player.actions_taken_this_play == 0


def test_row_state_requires_at_least_one_card() -> None:
    with pytest.raises(ValidationError):
        RowState(index=0, cards=())


def test_resolution_state_holds_ordered_cards_and_next_index() -> None:
    resolution = ResolutionState(
        ordered_cards=((3, "bob"), (45, "alice"), (46, "dan"), (53, "cara")),
        next_index=2,
        awaiting_player=None,
    )

    assert resolution.ordered_cards[2] == (46, "dan")
    assert resolution.awaiting_player is None


def test_match_state_holds_play_context() -> None:
    state = MatchState(
        match_id="m_01",
        phase=Phase.SELECTING,
        players=(PlayerState(player_id="alice", hand=(4, 19)),),
        rows=(
            RowState(index=0, cards=(3,)),
            RowState(index=1, cards=(45,)),
            RowState(index=2, cards=(52,)),
            RowState(index=3, cards=(88,)),
        ),
        hand_number=2,
        play_number=5,
    )

    assert state.hand_number == 2
    assert state.play_number == 5
    assert len(state.rows) == 4
    assert state.resolution is None


def test_state_models_are_immutable() -> None:
    player = PlayerState(player_id="alice", hand=(4,))

    with pytest.raises(ValidationError):
        player.committed = True  # ty: ignore[invalid-assignment]


def test_state_models_round_trip_through_json() -> None:
    state = MatchState(
        match_id="m_01",
        phase=Phase.SELECTING,
        players=(PlayerState(player_id="alice", hand=(4,)),),
        rows=(RowState(index=0, cards=(3,)),),
        hand_number=1,
        play_number=1,
    )

    restored = MatchState.model_validate_json(state.model_dump_json())

    assert restored == state
