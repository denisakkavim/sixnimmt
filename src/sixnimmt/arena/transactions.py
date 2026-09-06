"""Preflight bot transactions against immutable states without publishing events."""

from dataclasses import dataclass

from sixnimmt.arena.bots.base import ActionBatch, Rejection
from sixnimmt.engine.errors import EngineRejection, ErrorCode
from sixnimmt.engine.events import Event
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.state import MatchState
from sixnimmt.engine.transition import transition


@dataclass(frozen=True)
class PreparedBatch:
    steps: tuple[tuple[MatchState, list[Event]], ...]
    rejection: Rejection | None = None


def prepare_batch(
    state: MatchState, player_id: str, batch: ActionBatch, protocol: MatchProtocol, rules: GameRules
) -> PreparedBatch:
    steps = []
    terminal = False
    for index, action in enumerate(batch.actions):
        if terminal:
            message = (
                f"Game action {index + 1}: cannot follow a terminal action. Nothing was applied; memory is unchanged."
            )
            return PreparedBatch((), Rejection(ErrorCode.WRONG_PHASE, message, (), action))
        try:
            before = (state.hand_number, state.play_number, state.phase)
            state, events = transition(state, player_id, action, protocol, rules)
            after = (state.hand_number, state.play_number, state.phase)
            committed = next(player.committed for player in state.players if player.player_id == player_id)
            terminal = action.type in ("commit", "choose_row") or before != after or committed
            steps.append((state, events))
        except EngineRejection as error:
            message = f"Game action {index + 1} ({action.type.value}) failed: {error}. Nothing was applied; memory is unchanged."
            return PreparedBatch((), Rejection(error.code, message, (), action))
    return PreparedBatch(tuple(steps))
