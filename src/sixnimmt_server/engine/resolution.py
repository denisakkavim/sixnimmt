"""Card placement: tight fit, sixth-card capture, and the too-low pause."""

from sixnimmt_server.engine.cards import bull_heads
from sixnimmt_server.engine.errors import EngineRejection, ErrorCode
from sixnimmt_server.engine.events import (
    CardPlacedEvent,
    Event,
    RowChoiceMadeEvent,
    RowChoiceRequiredEvent,
    RowTakenEvent,
)
from sixnimmt_server.engine.state import MatchState, Phase, ResolutionState, RowState


def eligible_row(rows: tuple[RowState, ...], card: int) -> int | None:
    """Index of the eligible row with the highest end card, or None if too low."""
    best: int | None = None
    best_end = 0
    for row in rows:
        end = row.cards[-1]
        if end < card and (best is None or end > best_end):
            best = row.index
            best_end = end
    return best


def _capture_row(
    state: MatchState, card: int, player_id: str, row_index: int, reason: str
) -> tuple[MatchState, list[Event]]:
    row = next(row for row in state.rows if row.index == row_index)
    heads = sum(bull_heads(captured) for captured in row.cards)
    player_index = next(index for index, other in enumerate(state.players) if other.player_id == player_id)
    player = state.players[player_index]
    updated_player = player.model_copy(
        update={
            "penalty_cards": player.penalty_cards + row.cards,
            "score_this_hand": player.score_this_hand + heads,
        }
    )
    players = tuple(updated_player if index == player_index else other for index, other in enumerate(state.players))
    rows = tuple(
        RowState(index=other.index, cards=(card,)) if other.index == row_index else other for other in state.rows
    )
    events: list[Event] = [
        RowTakenEvent(
            match_id=state.match_id,
            hand=state.hand_number,
            play=state.play_number,
            audience="public",
            data={
                "player_id": player_id,
                "row": row_index,
                "captured": list(row.cards),
                "heads": heads,
                "reason": reason,
            },
        ),
        CardPlacedEvent(
            match_id=state.match_id,
            hand=state.hand_number,
            play=state.play_number,
            audience="public",
            data={"card": card, "row": row_index, "row_cards": [card]},
        ),
    ]
    new_state = state.model_copy(update={"players": players, "rows": rows})
    return new_state, events


def _append_card(state: MatchState, card: int, player_id: str, row_index: int) -> tuple[MatchState, list[Event]]:
    rows = tuple(
        RowState(index=row.index, cards=(*row.cards, card)) if row.index == row_index else row for row in state.rows
    )
    placed_row = next(row for row in rows if row.index == row_index)
    event: Event = CardPlacedEvent(
        match_id=state.match_id,
        hand=state.hand_number,
        play=state.play_number,
        audience="public",
        data={"card": card, "row": row_index, "row_cards": list(placed_row.cards)},
    )
    return state.model_copy(update={"rows": rows}), [event]


def _finish_card(state: MatchState, card: int, player_id: str, row_index: int) -> tuple[MatchState, list[Event]]:
    row = next(row for row in state.rows if row.index == row_index)
    if len(row.cards) == 5:
        return _capture_row(state, card, player_id, row_index, reason="sixth_card")
    return _append_card(state, card, player_id, row_index)


def _resolution_or_error(state: MatchState) -> ResolutionState:
    if state.resolution is None:
        msg = "no resolution in progress"
        raise EngineRejection(ErrorCode.WRONG_PHASE, msg)
    return state.resolution


def advance_resolution(state: MatchState) -> tuple[MatchState, list[Event]]:
    """Place the next card, or pause when it is too low for every row."""
    if state.phase != Phase.RESOLVING:
        msg = f"cannot resolve during {state.phase.value}"
        raise EngineRejection(ErrorCode.WRONG_PHASE, msg)
    resolution = _resolution_or_error(state)
    card, player_id = resolution.ordered_cards[resolution.next_index]
    target = eligible_row(state.rows, card)
    if target is None:
        paused = state.model_copy(
            update={
                "phase": Phase.AWAITING_ROW_CHOICE,
                "resolution": resolution.model_copy(update={"awaiting_player": player_id}),
            }
        )
        event: Event = RowChoiceRequiredEvent(
            match_id=state.match_id,
            hand=state.hand_number,
            play=state.play_number,
            audience=f"player:{player_id}",
            data={"player_id": player_id, "card": card},
        )
        return paused, [event]
    new_state, events = _finish_card(state, card, player_id, target)
    advanced = resolution.model_copy(update={"next_index": resolution.next_index + 1})
    return new_state.model_copy(update={"resolution": advanced}), events


def choose_row(state: MatchState, player_id: str, row_index: int) -> tuple[MatchState, list[Event]]:
    """Resolve a paused too-low card onto the chosen row and continue."""
    if state.phase != Phase.AWAITING_ROW_CHOICE:
        msg = f"no row choice is pending during {state.phase.value}"
        raise EngineRejection(ErrorCode.WRONG_PHASE, msg)
    resolution = _resolution_or_error(state)
    if resolution.awaiting_player != player_id:
        msg = f"the game is waiting for {resolution.awaiting_player} to choose a row"
        raise EngineRejection(ErrorCode.NOT_YOUR_TURN, msg)
    if not 0 <= row_index < len(state.rows):
        msg = f"row index {row_index} is out of range"
        raise EngineRejection(ErrorCode.INVALID_ROW_INDEX, msg)
    card, owner = resolution.ordered_cards[resolution.next_index]
    resumed, capture_events = _capture_row(state, card, owner, row_index, reason="too_low")
    chosen: Event = RowChoiceMadeEvent(
        match_id=state.match_id,
        hand=state.hand_number,
        play=state.play_number,
        audience="public",
        data={"player_id": player_id, "row": row_index},
    )
    taken, placed = capture_events
    continued = resumed.model_copy(
        update={
            "phase": Phase.RESOLVING,
            "resolution": resolution.model_copy(
                update={"awaiting_player": None, "next_index": resolution.next_index + 1}
            ),
        }
    )
    # The choice causes the capture, so it is recorded before it: clients replay
    # this stream to animate the play, and a sweep must never precede its cause.
    events: list[Event] = [chosen, taken, placed]
    while continued.phase == Phase.RESOLVING and continued.resolution is not None:
        if continued.resolution.next_index >= len(continued.resolution.ordered_cards):
            break
        continued, more_events = advance_resolution(continued)
        events.extend(more_events)
    return continued, events
