"""The only mutator: apply one player's action and return new state plus events."""

from sixnimmt_server.engine.actions import (
    Action,
    ChooseRowAction,
    CommitAction,
    SelectCardAction,
    SendMessageAction,
    UncommitAction,
)
from sixnimmt_server.engine.errors import EngineRejection, ErrorCode
from sixnimmt_server.engine.events import (
    CardsRevealedEvent,
    Event,
    PlayerCommittedEvent,
    SelectionMadeEvent,
    audience_for_player,
)
from sixnimmt_server.engine.resolution import advance_resolution
from sixnimmt_server.engine.resolution import choose_row as resolve_row_choice
from sixnimmt_server.engine.rules import GameRules, MatchProtocol
from sixnimmt_server.engine.state import MatchState, Phase, ResolutionState


def _find_player(state: MatchState, player_id: str) -> int:
    for index, player in enumerate(state.players):
        if player.player_id == player_id:
            return index
    msg = f"unknown player {player_id}"
    raise EngineRejection(ErrorCode.UNKNOWN_PLAYER, msg)


def _select_card(state: MatchState, player_index: int, action: SelectCardAction) -> tuple[MatchState, list[Event]]:
    player = state.players[player_index]
    if state.phase != Phase.SELECTING:
        msg = f"cannot select a card during {state.phase.value}"
        raise EngineRejection(ErrorCode.WRONG_PHASE, msg)
    if action.card not in player.hand:
        msg = f"card {action.card} is not in hand"
        raise EngineRejection(ErrorCode.CARD_NOT_IN_HAND, msg)
    remaining_hand = tuple(card for card in player.hand if card != action.card)
    updated = player.model_copy(update={"hand": remaining_hand, "selection": action.card, "committed": True})
    players = tuple(updated if index == player_index else other for index, other in enumerate(state.players))
    new_state = state.model_copy(update={"players": players})
    events: list[Event] = [
        SelectionMadeEvent(
            match_id=state.match_id,
            hand=state.hand_number,
            play=state.play_number,
            audience=audience_for_player(player.player_id),
            data={"player_id": player.player_id},
        ),
        PlayerCommittedEvent(
            match_id=state.match_id,
            hand=state.hand_number,
            play=state.play_number,
            audience="public",
            data={"player_id": player.player_id},
        ),
    ]
    if all(other.committed for other in players):
        ordered_pairs = []
        for other in players:
            if other.selection is None:
                msg = f"committed player {other.player_id} has no selection"
                raise EngineRejection(ErrorCode.NO_SELECTION_TO_COMMIT, msg)
            ordered_pairs.append((other.selection, other.player_id))
        ordered = tuple(sorted(ordered_pairs))
        new_state = new_state.model_copy(
            update={
                "phase": Phase.RESOLVING,
                "resolution": ResolutionState(ordered_cards=ordered),
            }
        )
        events.append(
            CardsRevealedEvent(
                match_id=state.match_id,
                hand=state.hand_number,
                play=state.play_number,
                audience="public",
                data={"selections": {other.player_id: other.selection for other in players}},
            )
        )
        revealed = tuple(sorted(other.selection for other in players if other.selection is not None))
        new_state = new_state.model_copy(update={"revealed_this_hand": (*new_state.revealed_this_hand, revealed)})
        current, resolution_events = advance_resolution(new_state)
        events.extend(resolution_events)
        while current.phase == Phase.RESOLVING and current.resolution is not None:
            if current.resolution.next_index >= len(current.resolution.ordered_cards):
                break
            current, more_events = advance_resolution(current)
            events.extend(more_events)
        return current, events
    return new_state, events


def _commit(state: MatchState, player_index: int, _: CommitAction) -> tuple[MatchState, list[Event]]:
    player = state.players[player_index]
    if state.phase != Phase.SELECTING:
        msg = f"cannot commit during {state.phase.value}"
        raise EngineRejection(ErrorCode.WRONG_PHASE, msg)
    if player.selection is None:
        msg = "cannot commit without a selected card"
        raise EngineRejection(ErrorCode.NO_SELECTION_TO_COMMIT, msg)
    msg = "committing is implicit in card selection"
    raise EngineRejection(ErrorCode.NEGOTIATION_DISABLED, msg)


def _reject_negotiation(_: MatchState, __: int, ___: UncommitAction | SendMessageAction) -> None:
    msg = "uncommit and messaging need negotiation, which is disabled"
    raise EngineRejection(ErrorCode.NEGOTIATION_DISABLED, msg)


def transition(
    state: MatchState,
    player_id: str,
    action: Action,
    protocol: MatchProtocol,
    rules: GameRules,
) -> tuple[MatchState, list[Event]]:
    """Apply one action. Rejections raise EngineRejection and change nothing."""
    if state.phase == Phase.FINISHED:
        msg = "match is finished"
        raise EngineRejection(ErrorCode.MATCH_FINISHED, msg)
    player_index = _find_player(state, player_id)
    if isinstance(action, ChooseRowAction):
        if state.phase != Phase.AWAITING_ROW_CHOICE:
            msg = f"no row choice is pending during {state.phase.value}"
            raise EngineRejection(ErrorCode.WRONG_PHASE, msg)
        return resolve_row_choice(state, player_id, action.row_index)
    if isinstance(action, SelectCardAction):
        return _select_card(state, player_index, action)
    if isinstance(action, CommitAction):
        return _commit(state, player_index, action)
    if isinstance(action, UncommitAction | SendMessageAction):
        _reject_negotiation(state, player_index, action)
    msg = f"unsupported action {action.type}"
    raise EngineRejection(ErrorCode.WRONG_PHASE, msg)
