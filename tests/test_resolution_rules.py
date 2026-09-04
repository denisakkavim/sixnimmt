"""Card resolution: tight fit, sixth-card capture, too-low pause, ordering."""

import pytest
from pydantic import TypeAdapter

from sixnimmt_server.engine.actions import Action, CommitAction
from sixnimmt_server.engine.cards import bull_heads
from sixnimmt_server.engine.errors import EngineRejection, ErrorCode
from sixnimmt_server.engine.resolution import advance_resolution, choose_row, eligible_row
from sixnimmt_server.engine.rules import GameRules, MatchProtocol
from sixnimmt_server.engine.state import MatchState, Phase, PlayerState, ResolutionState, RowState
from sixnimmt_server.engine.transition import transition

_ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)


def _rows(*ends: list[int]) -> tuple[RowState, ...]:
    return tuple(RowState(index=index, cards=tuple(cards)) for index, cards in enumerate(ends))


def _resolving(rows: tuple[RowState, ...], ordered: tuple[tuple[int, str], ...], next_index: int = 0) -> MatchState:
    players = tuple(PlayerState(player_id=player_id, hand=()) for _, player_id in ordered)
    return MatchState(
        match_id="m_01",
        phase=Phase.RESOLVING,
        players=players,
        rows=rows,
        resolution=ResolutionState(ordered_cards=ordered, next_index=next_index),
    )


def test_eligible_row_is_the_tightest_strictly_lower_fit() -> None:
    rows = _rows([3], [45], [52], [88])

    assert eligible_row(rows, 46) == 1
    assert eligible_row(rows, 89) == 3
    assert eligible_row(rows, 4) == 0


def test_equal_end_card_is_not_eligible() -> None:
    rows = _rows([44], [10], [20], [30])

    assert eligible_row(rows, 44) == 3


def test_no_eligible_row_returns_none() -> None:
    rows = _rows([7], [44], [52], [88])

    assert eligible_row(rows, 3) is None


def test_card_appends_to_eligible_row() -> None:
    state = _resolving(_rows([3], [45], [52], [88]), ((46, "dan"),))

    new_state, events = advance_resolution(state)

    assert [row.cards for row in new_state.rows] == [(3,), (45, 46), (52,), (88,)]
    assert new_state.resolution is not None and new_state.resolution.next_index == 1
    assert [event.type for event in events] == ["card_placed"]
    placed = events[0]
    assert placed.data == {"card": 46, "row": 1, "row_cards": [45, 46]}


def test_sixth_card_captures_the_full_row() -> None:
    state = _resolving(_rows([3], [23, 25, 30, 41, 44], [52], [88]), ((45, "alice"),))

    new_state, events = advance_resolution(state)

    assert [row.cards for row in new_state.rows] == [(3,), (45,), (52,), (88,)]
    alice = next(player for player in new_state.players if player.player_id == "alice")
    assert alice.penalty_cards == (23, 25, 30, 41, 44)
    assert alice.score_this_hand == sum(bull_heads(card) for card in (23, 25, 30, 41, 44)) == 12
    assert [event.type for event in events] == ["row_taken", "card_placed"]
    taken = events[0]
    assert taken.data["reason"] == "sixth_card"
    assert taken.data["heads"] == 12


def test_too_low_card_pauses_for_a_private_choice() -> None:
    state = _resolving(_rows([7], [44], [52], [88]), ((3, "bob"), (45, "alice")))

    new_state, events = advance_resolution(state)

    assert new_state.phase == Phase.AWAITING_ROW_CHOICE
    assert new_state.resolution is not None and new_state.resolution.awaiting_player == "bob"
    assert new_state.resolution.next_index == 0
    assert [event.type for event in events] == ["row_choice_required"]
    assert events[0].audience == "public"


def test_choice_captures_any_length_row_and_resumes() -> None:
    paused = MatchState(
        match_id="m_01",
        phase=Phase.AWAITING_ROW_CHOICE,
        players=(PlayerState(player_id="bob", hand=()),),
        rows=_rows([7], [23, 25, 30, 41, 44], [52], [88]),
        resolution=ResolutionState(ordered_cards=((3, "bob"),), next_index=0, awaiting_player="bob"),
    )

    new_state, events = choose_row(paused, "bob", 1)

    assert [row.cards for row in new_state.rows] == [(7,), (3,), (52,), (88,)]
    bob = new_state.players[0]
    assert bob.score_this_hand == 12
    assert new_state.phase == Phase.RESOLVING
    assert new_state.resolution is not None and new_state.resolution.awaiting_player is None
    assert [event.type for event in events] == ["row_choice_made", "row_taken", "card_placed"]
    assert events[1].data["reason"] == "too_low"


@pytest.mark.parametrize("chosen_row", [0, 1, 2, 3])
def test_every_row_is_a_legal_answer_to_a_too_low_card(chosen_row: int) -> None:
    rows = _rows([7], [23, 25, 30, 41, 44], [52], [88, 90])
    paused = MatchState(
        match_id="m_01",
        phase=Phase.AWAITING_ROW_CHOICE,
        players=(PlayerState(player_id="bob", hand=()),),
        rows=rows,
        resolution=ResolutionState(ordered_cards=((3, "bob"),), next_index=0, awaiting_player="bob"),
    )
    captured = rows[chosen_row].cards

    new_state, events = choose_row(paused, "bob", chosen_row)

    assert new_state.rows[chosen_row].cards == (3,)
    assert new_state.players[0].penalty_cards == captured
    assert new_state.players[0].score_this_hand == sum(bull_heads(card) for card in captured)
    assert [row.cards for index, row in enumerate(new_state.rows) if index != chosen_row] == [
        row.cards for index, row in enumerate(rows) if index != chosen_row
    ]
    assert [event.type for event in events] == ["row_choice_made", "row_taken", "card_placed"]


def test_choice_by_anyone_else_is_rejected() -> None:
    paused = MatchState(
        match_id="m_01",
        phase=Phase.AWAITING_ROW_CHOICE,
        players=(PlayerState(player_id="bob", hand=()),),
        rows=_rows([7], [44], [52], [88]),
        resolution=ResolutionState(ordered_cards=((3, "bob"),), next_index=0, awaiting_player="bob"),
    )

    with pytest.raises(EngineRejection) as exc_info:
        choose_row(paused, "alice", 0)

    assert exc_info.value.code == ErrorCode.NOT_YOUR_TURN


@pytest.mark.parametrize(
    ("player_id", "action"),
    [
        ("alice", _ACTION_ADAPTER.validate_python({"type": "choose_row", "row_index": 0})),
        ("alice", _ACTION_ADAPTER.validate_python({"type": "select_card", "card": 45})),
        ("bob", CommitAction()),
    ],
)
def test_only_awaited_players_row_choice_is_allowed_during_pause(
    player_id: str,
    action: Action,
) -> None:
    paused = MatchState(
        match_id="m_01",
        phase=Phase.AWAITING_ROW_CHOICE,
        players=(
            PlayerState(player_id="bob", hand=()),
            PlayerState(player_id="alice", hand=(45,)),
        ),
        rows=_rows([7], [44], [52], [88]),
        resolution=ResolutionState(ordered_cards=((3, "bob"),), next_index=0, awaiting_player="bob"),
    )

    with pytest.raises(EngineRejection) as exc_info:
        transition(paused, player_id, action, MatchProtocol(), GameRules())

    assert exc_info.value.code == ErrorCode.NOT_YOUR_TURN
    assert "bob" in str(exc_info.value)


def test_choice_outside_row_range_is_rejected() -> None:
    paused = MatchState(
        match_id="m_01",
        phase=Phase.AWAITING_ROW_CHOICE,
        players=(PlayerState(player_id="bob", hand=()),),
        rows=_rows([7], [44], [52], [88]),
        resolution=ResolutionState(ordered_cards=((3, "bob"),), next_index=0, awaiting_player="bob"),
    )

    with pytest.raises(EngineRejection) as exc_info:
        choose_row(paused, "bob", 9)

    assert exc_info.value.code == ErrorCode.INVALID_ROW_INDEX


def test_later_card_sees_the_board_left_by_earlier_cards() -> None:
    state = _resolving(_rows([10], [20], [30], [40]), ((25, "a"), (28, "b")))

    mid, _ = advance_resolution(state)
    assert [row.cards for row in mid.rows] == [(10,), (20, 25), (30,), (40,)]

    end, _ = advance_resolution(mid)
    assert [row.cards for row in end.rows] == [(10,), (20, 25, 28), (30,), (40,)]


def _example_state() -> MatchState:
    return MatchState(
        match_id="m_01",
        phase=Phase.SELECTING,
        players=(
            PlayerState(player_id="bob", hand=(3, 99)),
            PlayerState(player_id="alice", hand=(45, 100)),
            PlayerState(player_id="cara", hand=(53, 101)),
            PlayerState(player_id="dan", hand=(46, 102)),
        ),
        rows=_rows([7], [23, 25, 30, 41, 44], [52], [88]),
    )


def _select(state: MatchState, player_id: str, card: int) -> MatchState:
    action = _ACTION_ADAPTER.validate_python({"type": "select_card", "card": card})
    new_state, _ = transition(state, player_id, action, MatchProtocol(), GameRules())
    return new_state


def test_worked_example_reproduces_every_intermediate_board() -> None:
    state = _example_state()
    state = _select(state, "dan", 46)
    state = _select(state, "cara", 53)
    state = _select(state, "alice", 45)

    state, events = transition(
        state,
        "bob",
        _ACTION_ADAPTER.validate_python({"type": "select_card", "card": 3}),
        MatchProtocol(),
        GameRules(),
    )

    assert state.phase == Phase.AWAITING_ROW_CHOICE
    assert state.resolution is not None and state.resolution.awaiting_player == "bob"
    assert events[-1].type == "row_choice_required"
    assert state.revealed_this_hand == ((3, 45, 46, 53),)

    state, _ = transition(
        state,
        "bob",
        _ACTION_ADAPTER.validate_python({"type": "choose_row", "row_index": 0}),
        MatchProtocol(),
        GameRules(),
    )

    assert [row.cards for row in state.rows] == [(3,), (45, 46), (52, 53), (88,)]
    scores = {player.player_id: player.score_this_hand for player in state.players}
    assert scores == {"bob": 1, "alice": 12, "cara": 0, "dan": 0}
    hands = {player.player_id: player.hand for player in state.players}
    assert hands == {"bob": (99,), "alice": (100,), "cara": (101,), "dan": (102,)}
