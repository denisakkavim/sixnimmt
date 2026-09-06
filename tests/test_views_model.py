"""Role-filtered view shapes: what each viewer may see."""

import pytest
from pydantic import ValidationError

from sixnimmt.engine.state import Phase
from sixnimmt.engine.views import (
    MatchView,
    OpponentView,
    PlayerSelfView,
    RowView,
    ViewRole,
)


def test_view_roles_cover_all_supported_viewers() -> None:
    assert [role.value for role in ViewRole] == [
        "player",
        "public_spectator",
        "omniscient_observer",
        "admin",
    ]


def test_opponent_hides_selection_card_under_hidden_policy() -> None:
    opponent = OpponentView(
        player_id="bob",
        display_name="Player 2",
        cards_in_hand=6,
        has_selection=True,
        committed=True,
        selection=None,
        penalty_cards=(11, 30),
        score_this_hand=8,
        total_score=31,
    )

    assert opponent.has_selection is True
    assert opponent.selection is None


def test_player_view_shows_own_hand_and_hides_opponent_selection() -> None:
    view = MatchView(
        match_id="m_01",
        view_version=87,
        view_id="v_9f2c",
        status="in_progress",
        phase=Phase.SELECTING,
        hand_number=2,
        play_number=5,
        you=PlayerSelfView(
            player_id="alice",
            hand=(4, 19, 62, 77, 91, 103),
            selection=62,
            committed=False,
            penalty_cards=(23, 25, 30, 41, 44),
            score_this_hand=12,
            total_score=27,
            actions_taken_this_play=6,
            actions_remaining_this_play=None,
        ),
        rows=(
            RowView(index=0, cards=(3,)),
            RowView(index=1, cards=(45, 46)),
            RowView(index=2, cards=(52, 53)),
            RowView(index=3, cards=(88,)),
        ),
        players=(
            OpponentView(
                player_id="bob",
                display_name="Player 2",
                cards_in_hand=6,
                has_selection=True,
                committed=True,
                selection=None,
                penalty_cards=(11, 30),
                score_this_hand=8,
                total_score=31,
            ),
        ),
        revealed_this_hand=((7, 12, 40, 91), (2, 33, 58, 77)),
        awaiting=None,
        legal_actions=("select_card", "commit"),
        target_score=66,
    )

    assert view.view_version == 87
    assert view.you.hand == (4, 19, 62, 77, 91, 103)
    assert view.players[0].selection is None


def test_view_models_are_immutable() -> None:
    view = RowView(index=0, cards=(3,))

    with pytest.raises(ValidationError):
        view.index = 1  # ty: ignore[invalid-assignment]
