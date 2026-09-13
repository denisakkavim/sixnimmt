"""Compact factual outcomes and durable completion state for planned arenas."""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sixnimmt.arena.planning import ArenaPlan, SeatAssignment
from sixnimmt.arena.results import MatchOutcome


class MatchRecord(BaseModel):
    """Seat-aligned measurements; unfinished matches have no competitive scores."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    record_version: Literal[1] = 1
    job_id: str
    match_id: str
    seats: tuple[SeatAssignment, ...]
    outcome: MatchOutcome
    scores: tuple[int, ...] | None = None
    winners: tuple[int, ...] = ()
    completed_hand_scores: tuple[tuple[int, ...], ...] = ()
    partial_scores: tuple[int, ...] | None = None
    ended_by: int | None = None
    reason: str | None = None
    actions_accepted: int = Field(default=0, ge=0)
    actions_rejected: int = Field(default=0, ge=0)
    seat_actions: tuple[tuple[int, int], ...] = ()
    duration_seconds: float | None = Field(default=None, ge=0)
    seat_decision_seconds: tuple[float | None, ...] = ()
    seat_decision_calls: tuple[int, ...] = ()
    seat_decision_samples: tuple[tuple[float, ...], ...] = ()
    seat_stats: tuple[dict[str, Any] | None, ...] = ()
    stats_errors: dict[str, str] = Field(default_factory=dict)
    event_trace: str | None = None
    action_trace: str | None = None

    @property
    def completed_hands(self) -> int:
        return len(self.completed_hand_scores)

    @model_validator(mode="after")
    def _validate_scores(self) -> "MatchRecord":
        count = len(self.seats)
        if self.outcome != MatchOutcome.FINISHED:
            if self.scores is not None or len(self.winners) > 0:
                msg = "unfinished outcomes cannot carry competitive scores or winners"
                raise ValueError(msg)
        elif self.scores is None or len(self.scores) != count:
            msg = "finished outcomes require one score per seat"
            raise ValueError(msg)
        if self.scores is not None:
            lowest = min(self.scores)
            expected = tuple(index for index, score in enumerate(self.scores) if score == lowest)
            if self.winners != expected:
                msg = "winners must identify all lowest-score seats"
                raise ValueError(msg)
        if any(len(scores) != count for scores in self.completed_hand_scores):
            msg = "completed hands must have one score per seat"
            raise ValueError(msg)
        if self.partial_scores is not None and len(self.partial_scores) != count:
            msg = "partial scores must have one value per seat"
            raise ValueError(msg)
        if self.ended_by is not None and not 0 <= self.ended_by < count:
            msg = "responsible seat is outside the lineup"
            raise ValueError(msg)
        self._validate_measurements(count)
        return self

    def _validate_measurements(self, count: int) -> None:
        for values in (
            self.seat_actions,
            self.seat_decision_seconds,
            self.seat_decision_calls,
            self.seat_decision_samples,
            self.seat_stats,
        ):
            if len(values) not in (0, count):
                msg = "seat measurements must align with the lineup"
                raise ValueError(msg)


class RunStatus(BaseModel):
    """Started means submitted to a worker; lost jobs have no returned outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status_version: Literal[1] = 1
    state: Literal["running", "completed", "stopped", "failed"]
    planned_job_ids: tuple[str, ...]
    started_job_ids: tuple[str, ...] = ()
    completed_job_ids: tuple[str, ...] = ()
    lost_job_ids: tuple[str, ...] = ()
    unstarted_job_ids: tuple[str, ...] = ()
    decisions_abandoned: int = Field(default=0, ge=0)
    error: str | None = None


class ArenaRun(BaseModel):
    """A reloadable plan, its returned outcomes, and execution provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan: ArenaPlan
    results: tuple[MatchRecord, ...]
    status: RunStatus
    artifact_dir: Path | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
