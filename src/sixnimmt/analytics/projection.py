"""Shared report semantics, independent of terminal and Markdown formatting."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sixnimmt.analytics.models import Estimate, EstimateView, EvaluationReport
from sixnimmt.common.evaluation import RunStream


class ReportArtifacts(Protocol):
    """Published file paths supplied by the calling application."""

    @property
    def analysis(self) -> Path: ...

    @property
    def report(self) -> Path: ...


@dataclass(frozen=True)
class EstimateKey:
    configuration_id: str
    player_count: int
    population_id: str | None
    condition_id: str | None
    view: EstimateView
    stream: RunStream | None
    opponents: tuple[str, ...] | None
    copy_count: int | None
    seat: int | None
    permutation: str | None
    comparison_id: str | None
    reference_id: str | None

    @classmethod
    def from_estimate(cls, item: Estimate) -> "EstimateKey":
        return cls(
            item.configuration_id,
            item.player_count,
            item.population_id,
            item.condition_id,
            item.view,
            item.stream,
            item.opponents,
            item.copy_count,
            item.seat,
            item.permutation,
            item.comparison_id,
            item.reference_id,
        )


@dataclass(frozen=True)
class ResultRow:
    context: Estimate
    win: Estimate | None
    acceptable: Estimate | None


@dataclass(frozen=True)
class ComparisonKey:
    comparison_id: str | None
    configuration_id: str
    reference_id: str | None
    player_count: int
    population_id: str | None


def _display_label(label: str) -> str:
    words = label.replace("_", " ")
    return words[:1].upper() + words[1:]


def configuration_label(report: EvaluationReport, config_id: str) -> str:
    rendered = _display_label(report.catalogue.get(config_id, config_id))
    matching = [key for key, label in report.catalogue.items() if _display_label(label) == rendered]
    if len(matching) < 2:
        return rendered
    suffix = config_id[-6:]
    if sum(key[-6:] == suffix for key in matching) > 1:
        suffix = config_id
    return f"{rendered} ({suffix})"


def result_rows(estimates: Sequence[Estimate]) -> list[ResultRow]:
    groups: dict[EstimateKey, list[Estimate]] = defaultdict(list)
    for item in estimates:
        groups[EstimateKey.from_estimate(item)].append(item)
    return [
        ResultRow(
            items[0],
            next((item for item in items if item.objective == "win_credit"), None),
            next((item for item in items if item.objective == "acceptable_credit"), None),
        )
        for items in groups.values()
    ]


def average_score(report: EvaluationReport, item: Estimate) -> float | None:
    for profile in report.outcome_profiles:
        if (
            profile.configuration_id == item.configuration_id
            and profile.player_count == item.player_count
            and profile.population_id == item.population_id
            and profile.view == item.view
            and profile.opponents == item.opponents
        ):
            return profile.mean_penalty
    return None


def comparison_groups(estimates: Sequence[Estimate]) -> dict[ComparisonKey, list[Estimate]]:
    groups: dict[ComparisonKey, list[Estimate]] = defaultdict(list)
    for item in estimates:
        key = ComparisonKey(
            item.comparison_id, item.configuration_id, item.reference_id, item.player_count, item.population_id
        )
        groups[key].append(item)
    return dict(groups)


def population_groups(
    estimates: Sequence[Estimate],
) -> dict[tuple[int, str | None, EstimateView], list[Estimate]]:
    groups: dict[tuple[int, str | None, EstimateView], list[Estimate]] = defaultdict(list)
    for item in estimates:
        groups[(item.player_count, item.population_id, item.view)].append(item)
    return dict(groups)
