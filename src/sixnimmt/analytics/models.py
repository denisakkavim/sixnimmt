"""Serializable specifications and results for comparisons of arena runs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from sixnimmt.arena.planning import Population
from sixnimmt.common.evaluation import AnalysisSpec, EvidenceLabel, Objective, PlayerCount, RunStream
from sixnimmt.engine.rules import GameRules, MatchProtocol

EstimateView = Literal[
    "iid",
    "fixed",
    "composition",
    "copy_count",
    "seat_order",
    "weighted",
    "paired_replacement",
    "paired_condition_difference",
    "weakest_tested",
]
Interpretation = Literal["unresolved", "useful gain", "useful loss", "practical equivalence"]


class Interval(BaseModel):
    low: float
    high: float
    confidence_level: float
    method: str = "percentile bootstrap of independent samples, retaining related games and the sampling design"


class Coverage(BaseModel):
    planned_appearances: int
    started_appearances: int
    finished_appearances: int
    planned_blocks: int
    finished_blocks: int
    complete_blocks: int
    sampling_blocks: int = 0
    incomplete_pairs: int = 0
    completion_fraction: float | None


class CellSupport(BaseModel):
    opponents: tuple[str, ...]
    weight: float
    finished_appearances: int
    planned_appearances: int
    finished_blocks: int


class Estimate(BaseModel):
    estimate_id: str
    view: EstimateView
    configuration_id: str
    player_count: PlayerCount
    objective: Objective
    population_id: str | None = None
    condition_id: str | None = None
    stream: RunStream | None = None
    opponents: tuple[str, ...] | None = None
    copy_count: int | None = None
    seat: int | None = None
    permutation: str | None = None
    comparison_id: str | None = None
    reference_id: str | None = None
    value: float | None
    interval: Interval | None = None
    status: Literal["estimated", "insufficient_data", "unsupported"]
    interpretation: Interpretation | None = None
    coverage: Coverage
    missing_outcome_bounds: tuple[float, float] | None = None
    evidence_label: EvidenceLabel = "unspecified"
    cells: tuple[CellSupport, ...] = ()
    target_cell_count: int | None = None
    unlisted_cell_count: int = 0
    unlisted_cell_weight: float = 0.0
    notes: tuple[str, ...] = ()


class OutcomeProfile(BaseModel):
    configuration_id: str
    player_count: PlayerCount
    view: EstimateView
    population_id: str | None = None
    opponents: tuple[str, ...] | None = None
    finished_appearances: int
    finishing_distribution: tuple[float, ...] | None
    sole_win_frequency: float | None
    shared_first_frequency: float | None
    mean_penalty: float | None
    finished_hand_penalty: float | None
    completed_hand_appearances: int


class Diagnostics(BaseModel):
    planned_matches: int
    started_matches: int
    returned_matches: int
    finished_matches: int
    unsuccessful_matches: int
    missing_matches: int
    independent_blocks: int
    completed_blocks: int
    outcomes: dict[str, int]
    failure_reasons: dict[str, int]
    responsible_configurations: dict[str, int]
    actions_accepted: int
    actions_rejected: int
    measured_decision_calls: int
    decision_seconds_total: float | None
    decision_seconds_median: float | None
    decision_seconds_p95: float | None
    match_seconds_total: float | None
    resource_notes: tuple[str, ...] = ()


class ReportContext(BaseModel):
    """Recorded game and experimental context shared by report consumers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rules: GameRules
    protocol: MatchProtocol
    populations: tuple[Population, ...]
    player_counts: tuple[PlayerCount, ...]
    streams: tuple[RunStream, ...]


class EvaluationReport(BaseModel):
    analysis_version: str = "1"
    analysis_spec: AnalysisSpec
    run_status: Literal["running", "completed", "stopped", "failed"]
    artifact_dir: str | None = None
    catalogue: dict[str, str]
    context: ReportContext
    provenance: dict[str, JsonValue]
    diagnostics: Diagnostics
    population_estimates: tuple[Estimate, ...]
    composition_estimates: tuple[Estimate, ...]
    copy_count_estimates: tuple[Estimate, ...]
    seat_order_estimates: tuple[Estimate, ...]
    comparisons: tuple[Estimate, ...]
    condition_effects: tuple[Estimate, ...]
    weakest_cells: tuple[Estimate, ...]
    outcome_profiles: tuple[OutcomeProfile, ...]
    limitations: tuple[str, ...]
