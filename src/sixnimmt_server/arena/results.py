"""Match outcomes and aggregate counters, separate from authoritative game state."""

from dataclasses import dataclass, field
from enum import StrEnum

from sixnimmt_server.engine.events import Event
from sixnimmt_server.engine.state import MatchState


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

    @property
    def actions(self) -> int:
        return self.actions_accepted + self.actions_rejected


@dataclass(frozen=True)
class SeatResult:
    player_id: str
    bot_name: str
    wins: int
    ties: int
    total_score: int
    actions_accepted: int
    actions_rejected: int


@dataclass(frozen=True)
class ArenaResult:
    run_id: str = field(compare=False)
    seed: int
    games_requested: int
    games_started: int
    games_completed: int
    finished: int
    abandoned: int
    forfeited: int
    failed: int
    decisions_abandoned: int
    total_hands: int
    total_actions: int
    reproducible: bool
    players: tuple[SeatResult, ...]

    @property
    def games(self) -> int:
        return self.games_completed
