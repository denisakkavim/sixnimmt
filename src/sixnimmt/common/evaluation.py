"""Shared declarative settings for planning and analysing arena evidence."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

EvidenceLabel = Literal["unspecified", "development", "confirmation", "exploratory"]
Objective = Literal["win_credit", "acceptable_credit"]


class AnalysisSpec(BaseModel):
    """Declared estimands, resampling settings, and evidence provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    acceptable_positions: tuple[tuple[int, int], ...] = tuple((count, (count + 1) // 2) for count in range(2, 11))
    confidence_level: float = Field(default=0.95, gt=0, lt=1)
    bootstrap_samples: int = Field(default=1000, ge=0)
    resampling_seed: int = 0
    practical_effect_threshold: float = Field(default=0.02, ge=0, le=1)
    evidence_label: EvidenceLabel = "unspecified"
    evidence_references: tuple[str, ...] = ()
    configuration_ids: tuple[str, ...] = ()
    condition_ids: tuple[str, ...] = ()
    streams: tuple[str, ...] = ()
    player_counts: tuple[int, ...] = ()

    @model_validator(mode="after")
    def validate_cutoffs(self) -> Self:
        counts = [count for count, _ in self.acceptable_positions]
        if len(set(counts)) != len(counts):
            msg = "acceptable position cutoffs must have unique player counts"
            raise ValueError(msg)
        if any(count < 2 or count > 10 or cutoff < 1 or cutoff > count for count, cutoff in self.acceptable_positions):
            msg = "acceptable position cutoffs must fall within each player count"
            raise ValueError(msg)
        return self

    def cutoff(self, player_count: int) -> int:
        for count, cutoff in self.acceptable_positions:
            if count == player_count:
                return cutoff
        msg = f"no acceptable-position cutoff declared for {player_count} players"
        raise ValueError(msg)
