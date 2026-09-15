"""Shared declarative settings for planning and analysing arena evidence."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _exact_integer_version(value: object) -> int:
    if type(value) is not int:
        msg = "schema version must be an integer"
        raise ValueError(msg)
    return value


VersionOne = Annotated[Literal[1], BeforeValidator(_exact_integer_version)]
EvidenceLabel = Literal["unspecified", "development", "confirmation", "exploratory"]
Objective = Literal["win_credit", "acceptable_credit"]
RunStream = Literal["iid", "controlled", "matched", "fixed"]
PlayerCount = Annotated[int, Field(strict=True, ge=2, le=10)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]
PositiveCount = Annotated[int, Field(strict=True, ge=1)]


class AnalysisSpec(BaseModel):
    """Declared estimands, resampling settings, and evidence provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    acceptable_positions: tuple[tuple[PlayerCount, PositiveCount], ...] = tuple(
        (count, (count + 1) // 2) for count in range(2, 11)
    )
    confidence_level: float = Field(default=0.95, strict=True, gt=0, lt=1)
    bootstrap_samples: NonNegativeCount = 1000
    resampling_seed: int = Field(default=0, strict=True)
    practical_effect_threshold: float = Field(default=0.02, strict=True, ge=0, le=1)
    evidence_label: EvidenceLabel = "unspecified"
    evidence_references: tuple[str, ...] = ()
    configuration_ids: tuple[str, ...] = ()
    condition_ids: tuple[str, ...] = ()
    streams: tuple[RunStream, ...] = ()
    player_counts: tuple[PlayerCount, ...] = ()

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
