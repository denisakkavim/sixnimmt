"""Typed strategy evaluations, independent of match publication and presentation."""

from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict, runtime_checkable

from pydantic import JsonValue

from sixnimmt.arena.bots.base import Bot, DelegatingBot
from sixnimmt.arena.bots.uncertainty.options import PenaltyObjective


class ModelTextActivity(TypedDict):
    type: Literal["model_text", "reasoning_summary"]
    text: str
    item_id: str
    complete: bool


class ToolActivity(TypedDict):
    type: Literal["tool_activity"]
    text: str
    tool_name: str
    status: Literal["requested", "started", "running", "completed", "failed", "cancelled", "retrying"]
    item_id: str


ModelActivity = ModelTextActivity | ToolActivity


@dataclass(frozen=True)
class CandidateEvaluation:
    candidates: tuple[tuple[int, float], ...]
    objective: PenaltyObjective
    horizon_plays: int
    sample_count: int

    def as_activity(self) -> dict[str, JsonValue]:
        values: dict[str, JsonValue] = {str(card): value for card, value in self.candidates}
        return {
            "candidate_values": values,
            "objective": self.objective.model_dump(mode="json"),
            "horizon_plays": self.horizon_plays,
            "sample_count": self.sample_count,
        }


@runtime_checkable
class EvaluatingBot(Protocol):
    def decision_evaluation(self, cards_remaining: int) -> CandidateEvaluation | None: ...


def decision_evaluation(bot: Bot, cards_remaining: int) -> CandidateEvaluation | None:
    """Read the latest decision without calling cumulative statistics or providers."""
    if isinstance(bot, EvaluatingBot):
        return bot.decision_evaluation(cards_remaining)
    if isinstance(bot, DelegatingBot):
        return decision_evaluation(bot.delegate_bot(), cards_remaining)
    return None
