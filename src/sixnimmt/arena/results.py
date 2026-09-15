"""Match outcomes and public summaries, separate from authoritative game state."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypedDict

from sixnimmt.engine.errors import ErrorCode
from sixnimmt.engine.events import Event
from sixnimmt.engine.state import MatchState


class MatchOutcome(StrEnum):
    FINISHED = "finished"
    ABANDONED = "abandoned"
    FORFEITED = "forfeited"
    FAILED = "failed"


@dataclass(frozen=True)
class MatchResult:
    seed: int
    outcome: MatchOutcome
    final_state: MatchState
    events: tuple[Event, ...] = field(compare=False)
    winners: tuple[str, ...]
    actions_accepted: int
    actions_rejected: int
    ended_by: str | None = None
    reason: str | None = None
    seat_actions: tuple[tuple[int, int], ...] = ()
    # Privileged diagnostics: (player ID, lifecycle stage, exception detail).
    lifecycle_errors: tuple[tuple[str, str, str], ...] = ()

    @property
    def actions(self) -> int:
        return self.actions_accepted + self.actions_rejected


class PublicMatchSummary(TypedDict):
    outcome: str
    winners: tuple[str, ...]
    reason: str | None
    scores: dict[str, int]


def public_reason(reason: str | None) -> str | None:
    """Expose stable termination codes without leaking provider/private exceptions."""
    if reason is None:
        return None
    if reason in ("operator_stop", "decision_timeout", "match_action_limit", "play_action_limit"):
        return reason
    if reason in ErrorCode:
        return reason
    return "match_failed"


def public_summary(result: MatchResult) -> PublicMatchSummary:
    return {
        "outcome": result.outcome.value,
        "winners": result.winners,
        "reason": public_reason(result.reason),
        "scores": {player.player_id: player.total_score for player in result.final_state.players},
    }
