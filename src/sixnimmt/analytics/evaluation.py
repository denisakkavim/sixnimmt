"""Analyse compact arena evidence without executing bots or replaying traces."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple, NotRequired, TypedDict, Unpack

from sixnimmt.analytics.comparisons import comparison_estimates
from sixnimmt.analytics.diagnostics import analyse_diagnostics
from sixnimmt.analytics.inputs import Observation, observations
from sixnimmt.analytics.models import (
    AnalysisSpec,
    CellSupport,
    Coverage,
    Estimate,
    EstimateView,
    EvaluationReport,
    OutcomeProfile,
    ReportContext,
)
from sixnimmt.analytics.populations import population_weights
from sixnimmt.analytics.uncertainty import ratio_interval, weighted_interval
from sixnimmt.arena.artifacts import validate_run_evidence
from sixnimmt.common.evaluation import Objective, RunStream

if TYPE_CHECKING:
    from sixnimmt.arena.artifacts import ArenaRun
    from sixnimmt.arena.planning import Population


class ViewEstimates(NamedTuple):
    population: list[Estimate]
    composition: list[Estimate]
    copy_count: list[Estimate]
    seat_order: list[Estimate]


class ProfileContext(TypedDict):
    configuration_id: str
    player_count: int
    view: EstimateView
    population_id: NotRequired[str | None]
    opponents: NotRequired[tuple[str, ...]]


class EstimateContext(ProfileContext):
    condition_id: NotRequired[str | None]
    stream: NotRequired[RunStream]
    copy_count: NotRequired[int]
    seat: NotRequired[int]
    permutation: NotRequired[str]


def _coverage(rows: list[Observation], incomplete_pairs: int = 0) -> Coverage:
    blocks: dict[str, list[Observation]] = defaultdict(list)
    for row in rows:
        blocks[row.block].append(row)
    finished = sum(row.credits is not None for row in rows)
    return Coverage(
        planned_appearances=len(rows),
        started_appearances=sum(row.started for row in rows),
        finished_appearances=finished,
        planned_blocks=len(blocks),
        finished_blocks=sum(any(row.credits is not None for row in group) for group in blocks.values()),
        complete_blocks=sum(all(row.credits is not None for row in group) for group in blocks.values()),
        incomplete_pairs=incomplete_pairs,
        completion_fraction=finished / len(rows) if len(rows) > 0 else None,
    )


def _block_totals(
    rows: list[Observation], objective: Objective, universe: list[Observation] | None = None
) -> dict[str, tuple[str, float, int]]:
    source = universe if universe is not None else rows
    totals: dict[str, tuple[str, float, int]] = {row.block: (row.stratum, 0.0, 0) for row in source}
    for row in rows:
        value = row.value(objective)
        if value is None:
            continue
        _, numerator, denominator = totals.get(row.block, (row.stratum, 0.0, 0))
        totals[row.block] = (row.stratum, numerator + value, denominator + 1)
    return totals


def _estimate(
    rows: list[Observation],
    spec: AnalysisSpec,
    universe: list[Observation] | None = None,
    **context: Unpack[EstimateContext],
) -> tuple[Estimate, Estimate]:
    coverage = _coverage(rows)
    source = universe if universe is not None else rows
    coverage = coverage.model_copy(update={"sampling_blocks": len({row.block for row in source})})
    estimates: list[Estimate] = []
    for objective in ("win_credit", "acceptable_credit"):
        identity = repr((context, objective))
        blocks = _block_totals(rows, objective, universe)
        numerator = sum(value for _, value, _ in blocks.values())
        finished = coverage.finished_appearances
        value = numerator / finished if finished > 0 else None
        interval = ratio_interval(blocks, spec, identity)
        planned = coverage.planned_appearances
        bounds = (numerator / planned, (numerator + planned - finished) / planned) if planned > 0 else None
        estimate = Estimate(
            **context,
            estimate_id=identity,
            objective=objective,
            value=value,
            interval=interval,
            status="estimated" if interval is not None else "insufficient_data",
            coverage=coverage,
            missing_outcome_bounds=bounds,
            evidence_label=spec.evidence_label,
            notes=("Point estimate is conditional on finished appearances.",),
        )
        estimates.append(estimate)
    return estimates[0], estimates[1]


def _profile(rows: list[Observation], **context: Unpack[ProfileContext]) -> OutcomeProfile:
    finished = [row for row in rows if row.credits is not None]
    n = rows[0].job.player_count
    count = len(finished)
    distributions = [row.credits.finishing_distribution for row in finished if row.credits is not None]
    scores = [
        row.record.scores[row.seat] for row in finished if row.record is not None and row.record.scores is not None
    ]
    hand_scores = [
        score[row.seat] for row in finished if row.record is not None for score in row.record.completed_hand_scores
    ]
    return OutcomeProfile(
        **context,
        finished_appearances=count,
        finishing_distribution=tuple(sum(item[place] for item in distributions) / count for place in range(n))
        if count > 0
        else None,
        sole_win_frequency=sum(row.credits.sole_win for row in finished if row.credits is not None) / count
        if count > 0
        else None,
        shared_first_frequency=sum(row.credits.shared_first for row in finished if row.credits is not None) / count
        if count > 0
        else None,
        mean_penalty=sum(scores) / count if count > 0 else None,
        finished_hand_penalty=sum(hand_scores) / len(hand_scores) if len(hand_scores) > 0 else None,
        completed_hand_appearances=len(hand_scores),
    )


def _group_rows[Key](rows: list[Observation], key: Callable[[Observation], Key | None]) -> dict[Key, list[Observation]]:
    groups: dict[Key, list[Observation]] = defaultdict(list)
    for row in rows:
        identity = key(row)
        if identity is not None:
            groups[identity].append(row)
    return dict(groups)


def _iid_key(row: Observation) -> tuple[str, int, str | None] | None:
    if row.job.stream != "iid":
        return None
    return row.config_id, row.job.player_count, row.job.population_id


def _composition_key(row: Observation) -> tuple[str, int, tuple[str, ...]]:
    return row.config_id, row.job.player_count, row.opponents


def _copies_key(row: Observation) -> tuple[str, int, int]:
    return row.config_id, row.job.player_count, row.opponents.count(row.config_id) + 1


def _seats_key(row: Observation) -> tuple[str, int, str, int, int]:
    return row.config_id, row.job.player_count, row.job.condition_id, row.seat, row.job.permutation


def _fixed_key(row: Observation) -> tuple[str, int, str, int] | None:
    if row.job.stream != "fixed":
        return None
    return row.config_id, row.job.player_count, row.job.condition_id, row.seat


def _weighted_estimates(
    rows: list[Observation],
    population: Population,
    spec: AnalysisSpec,
    config_id: str,
    n: int,
    universe: list[Observation],
) -> tuple[Estimate, Estimate]:
    population_support = population_weights(population, n, {row.opponents for row in rows})
    weights = population_support.cell_weights
    cells: dict[tuple[str, ...], list[Observation]] = defaultdict(list)
    for row in rows:
        if row.opponents in weights:
            cells[row.opponents].append(row)
    support = tuple(
        CellSupport(
            opponents=opponents,
            weight=weight,
            finished_appearances=_coverage(cells[opponents]).finished_appearances,
            planned_appearances=len(cells[opponents]),
            finished_blocks=_coverage(cells[opponents]).finished_blocks,
        )
        for opponents, weight in weights.items()
    )
    selected = [row for group in cells.values() for row in group]
    coverage = _coverage(selected)
    supported = (
        population_support.target_cell_count > 0
        and population_support.unlisted_cell_count == 0
        and all(cell.finished_appearances > 0 for cell in support)
    )
    estimates: list[Estimate] = []
    for objective in ("win_credit", "acceptable_credit"):
        identity = f"weighted:{population.population_id}:{config_id}:{n}:{objective}"
        value = 0.0
        lower = 0.0
        upper = population_support.unlisted_cell_weight
        blocks: dict[str, tuple[str, dict[tuple[str, ...], tuple[float, int]]]] = {
            row.block: (row.stratum, {}) for row in universe if row.job.stream == "iid" and row.job.player_count == n
        }
        for opponents, weight in weights.items():
            observed = [row.value(objective) for row in cells[opponents] if row.credits is not None]
            total = sum(item for item in observed if item is not None)
            if len(observed) > 0:
                value += weight * total / len(observed)
            planned = len(cells[opponents])
            lower += weight * total / planned if planned > 0 else 0.0
            upper += weight * (total + planned - len(observed)) / planned if planned > 0 else weight
            for block, (stratum, numerator, denominator) in _block_totals(cells[opponents], objective).items():
                if block not in blocks:
                    blocks[block] = (stratum, {})
                blocks[block][1][opponents] = (numerator, denominator)
        interval = weighted_interval(blocks, weights, spec, identity) if supported else None
        estimates.append(
            Estimate(
                estimate_id=identity,
                view="weighted",
                configuration_id=config_id,
                player_count=n,
                objective=objective,
                population_id=population.population_id,
                value=value if supported else None,
                interval=interval,
                status="unsupported" if not supported else "estimated" if interval is not None else "insufficient_data",
                coverage=coverage,
                missing_outcome_bounds=(lower, upper) if population_support.target_cell_count > 0 else None,
                evidence_label=spec.evidence_label,
                cells=support,
                target_cell_count=population_support.target_cell_count,
                unlisted_cell_count=population_support.unlisted_cell_count,
                unlisted_cell_weight=population_support.unlisted_cell_weight,
                notes=("Declared population weights are retained; no missing cells are filled or renormalised.",),
            )
        )
    return estimates[0], estimates[1]


def _view_estimates(
    rows: list[Observation], spec: AnalysisSpec, universe: list[Observation], config_ids: list[str]
) -> ViewEstimates:
    population: list[Estimate] = []
    composition: list[Estimate] = []
    copies: list[Estimate] = []
    seats: list[Estimate] = []
    iid_groups = _group_rows(rows, _iid_key)
    source_groups: dict[tuple[int, str | None], list[Observation]] = defaultdict(list)
    for row in universe:
        if row.job.stream == "iid":
            source_groups[(row.job.player_count, row.job.population_id)].append(row)
    for (n, population_id), source in source_groups.items():
        for config_id in config_ids:
            group = iid_groups.get((config_id, n, population_id), [])
            population.extend(
                _estimate(
                    group,
                    spec,
                    universe=source,
                    view="iid",
                    configuration_id=config_id,
                    player_count=n,
                    population_id=population_id,
                    stream="iid",
                )
            )
    for (config_id, n, condition_id, seat), group in _group_rows(rows, _fixed_key).items():
        population.extend(
            _estimate(
                group,
                spec,
                view="fixed",
                configuration_id=config_id,
                player_count=n,
                condition_id=condition_id,
                seat=seat,
                stream="fixed",
            )
        )
    for (config_id, n, opponents), group in _group_rows(rows, _composition_key).items():
        composition.extend(
            _estimate(group, spec, view="composition", configuration_id=config_id, player_count=n, opponents=opponents)
        )
    for (config_id, n, count), group in _group_rows(rows, _copies_key).items():
        copies.extend(
            _estimate(group, spec, view="copy_count", configuration_id=config_id, player_count=n, copy_count=count)
        )
    for (config_id, n, condition_id, seat, permutation), group in _group_rows(rows, _seats_key).items():
        seats.extend(
            _estimate(
                group,
                spec,
                view="seat_order",
                configuration_id=config_id,
                player_count=n,
                condition_id=condition_id,
                seat=seat,
                permutation=str(permutation),
            )
        )
    return ViewEstimates(population, composition, copies, seats)


def _profiles(rows: list[Observation]) -> tuple[OutcomeProfile, ...]:
    profiles: list[OutcomeProfile] = []
    for (config_id, n, population_id), group in _group_rows(rows, _iid_key).items():
        profiles.append(
            _profile(group, configuration_id=config_id, player_count=n, view="iid", population_id=population_id)
        )
    for (config_id, n, opponents), group in _group_rows(rows, _composition_key).items():
        profiles.append(
            _profile(group, configuration_id=config_id, player_count=n, view="composition", opponents=opponents)
        )
    return tuple(profiles)


def _weakest(estimates: list[Estimate]) -> tuple[Estimate, ...]:
    groups: dict[tuple[str, int, Objective], list[Estimate]] = defaultdict(list)
    for estimate in estimates:
        if estimate.value is not None:
            groups[(estimate.configuration_id, estimate.player_count, estimate.objective)].append(estimate)
    return tuple(
        min(group, key=_estimate_value).model_copy(
            update={
                "view": "weakest_tested",
                "evidence_label": "exploratory",
                "notes": ("Selected minimum among tested cells; selection can exaggerate a vulnerability.",),
            }
        )
        for group in groups.values()
    )


def _estimate_value(estimate: Estimate) -> float:
    return estimate.value if estimate.value is not None else float("inf")


def analyse_run(run: ArenaRun, analysis_spec: AnalysisSpec | None = None) -> EvaluationReport:
    """Derive all views from a returned run or one reconstructed by ``load_run``."""
    validate_run_evidence(run.plan, run.results, run.status)
    spec = analysis_spec if analysis_spec is not None else run.plan.analysis
    universe = observations(run, spec)
    rows = [row for row in universe if len(spec.configuration_ids) == 0 or row.config_id in spec.configuration_ids]
    config_ids = [
        entry.config_id
        for entry in run.plan.catalogue
        if len(spec.configuration_ids) == 0 or entry.config_id in spec.configuration_ids
    ]
    populations, compositions, copies, seats = _view_estimates(rows, spec, universe, config_ids)
    by_candidate: dict[tuple[str, int], list[Observation]] = defaultdict(list)
    for row in rows:
        by_candidate[(row.config_id, row.job.player_count)].append(row)
    for population in run.plan.populations:
        for config_id in config_ids:
            for count in sorted({row.job.player_count for row in universe}):
                group = by_candidate[(config_id, count)]
                populations.extend(_weighted_estimates(group, population, spec, config_id, count, universe))
    comparisons, effects = comparison_estimates(rows, run.plan, spec)
    diagnostics = analyse_diagnostics(run, universe)
    limitations = [
        "Point estimates use finished outcomes. Missing-outcome bounds permit any credit from zero to one for missing appearances.",
        "Intervals resample independent samples while keeping games that share a deal or a randomly selected opponent lineup together.",
        "Deliberately chosen lineups retain their planned sampling proportions. Too few independent results, or missing required combinations, leave the interval unavailable.",
        "Intervals are marginal, without a multiple-comparison adjustment. Selected subgroup and weakest-cell findings are exploratory.",
        "Population credits describe the named bot population; they do not establish performance against a human group.",
    ]
    if any(row.job.stream == "fixed" for row in rows):
        limitations.append(
            "Fixed-lineup results describe the named seats against that lineup and cannot establish general strength."
        )
    if diagnostics.missing_matches > 0 or diagnostics.unsuccessful_matches > 0:
        limitations.append(
            "Incomplete outcomes can change rankings and leave prespecified pairs incomplete; review coverage and bounds."
        )
    return EvaluationReport(
        analysis_spec=spec,
        run_status=run.status.state,
        artifact_dir=str(run.artifact_dir) if run.artifact_dir is not None else None,
        catalogue={entry.config_id: entry.label for entry in run.plan.catalogue},
        context=ReportContext(
            rules=run.plan.rules,
            protocol=run.plan.protocol,
            populations=run.plan.populations,
            player_counts=tuple(sorted({row.job.player_count for row in rows})),
            streams=tuple(sorted({row.job.stream for row in rows})),
        ),
        provenance=run.provenance,
        diagnostics=diagnostics,
        population_estimates=tuple(populations),
        composition_estimates=tuple(compositions),
        copy_count_estimates=tuple(item.model_copy(update={"evidence_label": "exploratory"}) for item in copies),
        seat_order_estimates=tuple(item.model_copy(update={"evidence_label": "exploratory"}) for item in seats),
        comparisons=tuple(comparisons),
        condition_effects=tuple(effects),
        weakest_cells=_weakest(compositions),
        outcome_profiles=_profiles(rows),
        limitations=tuple(limitations),
    )
