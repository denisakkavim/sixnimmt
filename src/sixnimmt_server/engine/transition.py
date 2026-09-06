"""The only mutator: apply one player's action and return new state plus events."""

from sixnimmt_server.engine.actions import (
    Action,
    ChooseRowAction,
    CommitAction,
    MessageVisibility,
    SelectCardAction,
    SendMessageAction,
    UncommitAction,
)
from sixnimmt_server.engine.errors import EngineRejection, ErrorCode
from sixnimmt_server.engine.events import (
    ActionCountedEvent,
    CardsRevealedEvent,
    Event,
    MessageSentEvent,
    PlayCommittedEvent,
    PlayerCommittedEvent,
    PlayerUncommittedEvent,
    PrivateMessageOccurredEvent,
    SelectionClearedEvent,
    SelectionMadeEvent,
    SelectionRegisteredEvent,
    audience_for_player,
)
from sixnimmt_server.engine.lifecycle import end_play
from sixnimmt_server.engine.resolution import advance_resolution
from sixnimmt_server.engine.resolution import choose_row as resolve_row_choice
from sixnimmt_server.engine.rules import GameRules, MatchProtocol, PrivateMessageExistence
from sixnimmt_server.engine.state import MatchState, Phase, ResolutionState


def _find_player(state: MatchState, player_id: str) -> int:
    for index, player in enumerate(state.players):
        if player.player_id == player_id:
            return index
    msg = f"unknown player {player_id}"
    raise EngineRejection(ErrorCode.UNKNOWN_PLAYER, msg)


def _replace_player(state: MatchState, player_index: int, **updates: object) -> MatchState:
    updated = state.players[player_index].model_copy(update=updates)
    players = tuple(updated if index == player_index else player for index, player in enumerate(state.players))
    return state.model_copy(update={"players": players})


def _check_action_budget(state: MatchState, player_index: int, protocol: MatchProtocol) -> None:
    maximum = protocol.max_actions_per_play
    if maximum is None:
        return
    player = state.players[player_index]
    if player.actions_taken_this_play < maximum:
        return
    msg = f"player {player.player_id} has used all {maximum} actions for this play"
    raise EngineRejection(ErrorCode.ACTION_BUDGET_EXHAUSTED, msg)


def _count_action(
    state: MatchState, player_index: int, protocol: MatchProtocol
) -> tuple[MatchState, ActionCountedEvent]:
    player = state.players[player_index]
    count = player.actions_taken_this_play + 1
    counted = _replace_player(state, player_index, actions_taken_this_play=count)
    remaining = None if protocol.max_actions_per_play is None else protocol.max_actions_per_play - count
    # Counts stay private even with a budget: publishing remaining actions would
    # reveal hidden messages. This resolves the conflicting public-budget rule.
    event = ActionCountedEvent(
        match_id=state.match_id,
        hand=state.hand_number,
        play=state.play_number,
        audience=audience_for_player(player.player_id),
        data={
            "player_id": player.player_id,
            "actions_taken_this_play": count,
            "actions_remaining_this_play": remaining,
        },
    )
    return counted, event


def _selection_event(state: MatchState, player_id: str, card: int) -> Event:
    return SelectionMadeEvent(
        match_id=state.match_id,
        hand=state.hand_number,
        play=state.play_number,
        audience=audience_for_player(player_id),
        data={"player_id": player_id, "card": card},
    )


def _public_player_event(state: MatchState, event_type: type[Event], player_id: str) -> Event:
    return event_type(
        match_id=state.match_id,
        hand=state.hand_number,
        play=state.play_number,
        audience="public",
        data={"player_id": player_id},
    )


def _begin_resolution(state: MatchState) -> tuple[MatchState, list[Event]]:
    ordered_pairs: list[tuple[int, str]] = []
    revealed: dict[str, int] = {}
    players = []
    scores_before_play = []

    for player in state.players:
        if player.selection is None:
            msg = f"committed player {player.player_id} has no selection"
            raise EngineRejection(ErrorCode.NO_SELECTION_TO_COMMIT, msg)
        hand = list(player.hand)
        hand.remove(player.selection)
        players.append(
            player.model_copy(
                update={
                    "hand": tuple(hand),
                    "selection": None,
                    "committed": False,
                }
            )
        )
        ordered_pairs.append((player.selection, player.player_id))
        revealed[player.player_id] = player.selection
        scores_before_play.append((player.player_id, player.score_this_hand))

    ordered = tuple(sorted(ordered_pairs))
    revealed_cards = tuple(card for card, _ in ordered)
    resolving = state.model_copy(
        update={
            "phase": Phase.RESOLVING,
            "players": tuple(players),
            "resolution": ResolutionState(
                ordered_cards=ordered,
                scores_before_play=tuple(scores_before_play),
            ),
            "revealed_this_hand": (*state.revealed_this_hand, revealed_cards),
        }
    )
    events: list[Event] = [
        PlayCommittedEvent(
            match_id=state.match_id,
            hand=state.hand_number,
            play=state.play_number,
            audience="public",
            data={},
        ),
        CardsRevealedEvent(
            match_id=state.match_id,
            hand=state.hand_number,
            play=state.play_number,
            audience="public",
            data={"selections": revealed},
        ),
    ]
    return resolving, events


def _drain_resolution(
    state: MatchState,
    rules: GameRules,
    protocol: MatchProtocol,
) -> tuple[MatchState, list[Event]]:
    current = state
    events: list[Event] = []
    while current.phase == Phase.RESOLVING and current.resolution is not None:
        if current.resolution.next_index >= len(current.resolution.ordered_cards):
            closed, closing_events = end_play(current, rules, protocol)
            events.extend(closing_events)
            return closed, events
        current, more_events = advance_resolution(current)
        events.extend(more_events)
    return current, events


def _finish_commitment(
    state: MatchState,
    events: list[Event],
    rules: GameRules,
    protocol: MatchProtocol,
) -> tuple[MatchState, list[Event]]:
    if not all(player.committed for player in state.players):
        return state, events
    resolving, commitment_events = _begin_resolution(state)
    current, resolution_events = _drain_resolution(resolving, rules, protocol)
    return current, [*events, *commitment_events, *resolution_events]


def _select_card(
    state: MatchState,
    player_index: int,
    action: SelectCardAction,
    protocol: MatchProtocol,
    rules: GameRules,
) -> tuple[MatchState, list[Event]]:
    if state.phase != Phase.SELECTING:
        msg = f"cannot select a card during {state.phase.value}"
        raise EngineRejection(ErrorCode.WRONG_PHASE, msg)
    player = state.players[player_index]
    if action.card not in player.hand:
        msg = f"card {action.card} is not in hand"
        raise EngineRejection(ErrorCode.CARD_NOT_IN_HAND, msg)
    _check_action_budget(state, player_index, protocol)

    counted, count_event = _count_action(state, player_index, protocol)
    selected = _replace_player(counted, player_index, selection=action.card, committed=False)
    events: list[Event] = [count_event]
    # Replacing a selection always clears the old one publicly, even when the
    # new card is the same. Emitting different events for a changed card would
    # tell opponents whether a hidden selection actually moved.
    if player.selection is not None:
        events.append(_public_player_event(state, SelectionClearedEvent, player.player_id))
    if player.committed:
        events.append(_public_player_event(state, PlayerUncommittedEvent, player.player_id))
    events.extend([
        _selection_event(state, player.player_id, action.card),
        _public_player_event(state, SelectionRegisteredEvent, player.player_id),
    ])

    if protocol.communication_enabled:
        return selected, events

    committed = _replace_player(selected, player_index, committed=True)
    events.append(_public_player_event(state, PlayerCommittedEvent, player.player_id))
    return _finish_commitment(committed, events, rules, protocol)


def _commit(
    state: MatchState,
    player_index: int,
    protocol: MatchProtocol,
    rules: GameRules,
) -> tuple[MatchState, list[Event]]:
    if state.phase != Phase.SELECTING:
        msg = f"cannot commit during {state.phase.value}"
        raise EngineRejection(ErrorCode.WRONG_PHASE, msg)
    player = state.players[player_index]
    if player.selection is None:
        msg = "cannot commit without a selected card"
        raise EngineRejection(ErrorCode.NO_SELECTION_TO_COMMIT, msg)
    if not protocol.communication_enabled:
        msg = "committing is implicit in card selection"
        raise EngineRejection(ErrorCode.COMMUNICATION_DISABLED, msg)
    if player.committed:
        msg = f"player {player.player_id} is already committed"
        raise EngineRejection(ErrorCode.WRONG_PHASE, msg)
    _check_action_budget(state, player_index, protocol)

    counted, count_event = _count_action(state, player_index, protocol)
    committed = _replace_player(counted, player_index, committed=True)
    events = [count_event, _public_player_event(state, PlayerCommittedEvent, player.player_id)]
    return _finish_commitment(committed, events, rules, protocol)


def _uncommit(
    state: MatchState,
    player_index: int,
    protocol: MatchProtocol,
) -> tuple[MatchState, list[Event]]:
    if not protocol.communication_enabled:
        msg = "uncommit needs communication, which is disabled"
        raise EngineRejection(ErrorCode.COMMUNICATION_DISABLED, msg)
    if state.phase != Phase.SELECTING:
        msg = f"cannot uncommit during {state.phase.value}"
        raise EngineRejection(ErrorCode.WRONG_PHASE, msg)
    player = state.players[player_index]
    if not player.committed:
        msg = f"player {player.player_id} is not committed"
        raise EngineRejection(ErrorCode.WRONG_PHASE, msg)
    if all(other.committed for other in state.players):
        msg = "cannot uncommit after every player has committed"
        raise EngineRejection(ErrorCode.CANNOT_UNCOMMIT_WHEN_ALL_COMMITTED, msg)
    _check_action_budget(state, player_index, protocol)

    counted, count_event = _count_action(state, player_index, protocol)
    uncommitted = _replace_player(counted, player_index, selection=None, committed=False)
    events = [
        count_event,
        _public_player_event(state, PlayerUncommittedEvent, player.player_id),
        _public_player_event(state, SelectionClearedEvent, player.player_id),
    ]
    return uncommitted, events


def _choose_row_and_continue(
    state: MatchState,
    player_id: str,
    row_index: int,
    rules: GameRules,
    protocol: MatchProtocol,
) -> tuple[MatchState, list[Event]]:
    current, events = resolve_row_choice(state, player_id, row_index)
    if current.phase != Phase.RESOLVING or current.resolution is None:
        return current, events
    if current.resolution.next_index < len(current.resolution.ordered_cards):
        return current, events
    closed, closing_events = end_play(current, rules, protocol)
    return closed, [*events, *closing_events]


def _check_message_recipient(
    state: MatchState, sender: str, action: SendMessageAction, protocol: MatchProtocol
) -> None:
    if action.visibility != MessageVisibility.DIRECT:
        return
    if action.to_player not in [player.player_id for player in state.players]:
        msg = f"there is no player {action.to_player!a} in this match"
        raise EngineRejection(ErrorCode.RECIPIENT_NOT_FOUND, msg)
    # The view fold deduplicates direct copies by addressee; distinct parties
    # are required for that rule to yield exactly one entry per message.
    if action.to_player == sender:
        msg = "direct messages must address another player"
        raise EngineRejection(ErrorCode.MALFORMED_REQUEST, msg)
    if not protocol.allow_direct_messages:
        msg = "this match allows table messages only"
        raise EngineRejection(ErrorCode.DIRECT_MESSAGES_DISABLED, msg)


def _send_message(
    state: MatchState, player_index: int, action: SendMessageAction, protocol: MatchProtocol
) -> tuple[MatchState, list[Event]]:
    if not protocol.communication_enabled:
        msg = "messaging needs communication, which is disabled"
        raise EngineRejection(ErrorCode.COMMUNICATION_DISABLED, msg)
    if state.phase != Phase.SELECTING:
        msg = f"cannot send messages during {state.phase.value}"
        raise EngineRejection(ErrorCode.WRONG_PHASE, msg)
    sender = state.players[player_index].player_id
    _check_message_recipient(state, sender, action, protocol)
    if len(action.body) > protocol.max_message_length:
        msg = f"your message is {len(action.body)} characters; the limit is {protocol.max_message_length}"
        raise EngineRejection(ErrorCode.MESSAGE_TOO_LONG, msg)
    _check_action_budget(state, player_index, protocol)
    counted, count_event = _count_action(state, player_index, protocol)
    data = {"from": sender, "visibility": action.visibility.value, "body": action.body}
    events: list[Event] = [count_event]
    if action.visibility == MessageVisibility.TABLE:
        events.append(
            MessageSentEvent(
                match_id=state.match_id, hand=state.hand_number, play=state.play_number, audience="public", data=data
            )
        )
        return counted, events
    recipient = action.to_player
    assert recipient is not None  # noqa: S101 — validated by SendMessageAction.
    data["to"] = recipient
    for party in (sender, recipient):
        events.append(
            MessageSentEvent(
                match_id=state.match_id,
                hand=state.hand_number,
                play=state.play_number,
                audience=audience_for_player(party),
                data=dict(data),
            )
        )
    if protocol.information_policy.private_message_existence == PrivateMessageExistence.VISIBLE:
        events.append(
            PrivateMessageOccurredEvent(
                match_id=state.match_id,
                hand=state.hand_number,
                play=state.play_number,
                audience="public",
                data={"from": sender, "to": recipient},
            )
        )
    return counted, events


def _awaited_player(state: MatchState) -> str:
    if state.resolution is None or state.resolution.awaiting_player is None:
        msg = "row-choice phase has no awaited player"
        raise EngineRejection(ErrorCode.WRONG_PHASE, msg)
    return state.resolution.awaiting_player


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

    if state.phase == Phase.AWAITING_ROW_CHOICE:
        awaited = _awaited_player(state)
        if not isinstance(action, ChooseRowAction) or player_id != awaited:
            msg = f"the game is waiting for {awaited} to choose a row"
            raise EngineRejection(ErrorCode.NOT_YOUR_TURN, msg)
        return _choose_row_and_continue(state, player_id, action.row_index, rules, protocol)

    if isinstance(action, ChooseRowAction):
        msg = f"no row choice is pending during {state.phase.value}"
        raise EngineRejection(ErrorCode.WRONG_PHASE, msg)

    if isinstance(action, SelectCardAction):
        return _select_card(state, player_index, action, protocol, rules)
    if isinstance(action, CommitAction):
        return _commit(state, player_index, protocol, rules)
    if isinstance(action, UncommitAction):
        return _uncommit(state, player_index, protocol)
    if isinstance(action, SendMessageAction):
        return _send_message(state, player_index, action, protocol)
    msg = f"unsupported action {action.type}"
    raise EngineRejection(ErrorCode.WRONG_PHASE, msg)
