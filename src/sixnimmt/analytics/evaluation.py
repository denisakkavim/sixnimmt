"""Analyse compact arena evidence without executing bots or replaying traces."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sixnimmt.analytics.comparisons import comparison_estimates
from sixnimmt.analytics.diagnostics import analyse_diagnostics
from sixnimmt.analytics.metrics import FinishCredits, finish_credits
from sixnimmt.analytics.models import AnalysisSpec, CellSupport, Coverage, Estimate, EvaluationReport, OutcomeProfile
from sixnimmt.analytics.populations import population_weights
from sixnimmt.analytics.uncertainty import ratio_interval, weighted_interval

if TYPE_CHECKING:
    from sixnimmt.arena.planning import PlannedMatch, Population
    from sixnimmt.arena.records import ArenaRun, MatchRecord


@dataclass(frozen=True)
class Observation:
    job: PlannedMatch
    record: MatchRecord | None
    config_id: str
    seat: int
    opponents: tuple[str, ...]
    block: str
    stratum: str
    started: bool
    credits: FinishCredits | None

    def value(self, objective: str) -> float | None:
        if self.credits is None:
            return None
        return self.credits.win_credit if objective == "win_credit" else self.credits.acceptable_credit


def _selected(job: PlannedMatch, spec: AnalysisSpec) -> bool:
    return (
        (len(spec.player_counts) == 0 or job.player_count in spec.player_counts)
        and (len(spec.condition_ids) == 0 or job.condition_id in spec.condition_ids)
        and (len(spec.streams) == 0 or job.stream in spec.streams)
    )


def _clusters(jobs: tuple[PlannedMatch, ...]) -> tuple[dict[str, str], dict[str, str]]:
    """Take the transitive closure of the explicitly declared dependencies."""
    parents: dict[str, str] = {job.job_id: job.job_id for job in jobs}
    seen: dict[tuple[int, str, str], str] = {}
    for job in jobs:
        identities = [("block", job.block_id), ("deal", job.shared_deal_id)]
        if job.lineup_draw_id is not None and job.stream in ("iid", "matched"):
            identities.append(("lineup", job.lineup_draw_id))
        for kind, identity in identities:
            if identity is None:
                continue
            key = (job.player_count, kind, identity)
            if key in seen:
                parents[_root(parents, job.job_id)] = _root(parents, seen[key])
            else:
                seen[key] = job.job_id
    clusters = {job.job_id: _root(parents, job.job_id) for job in jobs}
    designs: dict[str, set[str]] = defaultdict(set)
    for job in jobs:
        # Fixed quotas condition on the declared composition. Shared deals are
        # resampled once with their entire set of controlled conditions.
        design = job.stream
        if job.stream in ("controlled", "fixed") or (job.stream == "matched" and job.lineup_draw_id is None):
            design += ":" + job.condition_id
        designs[clusters[job.job_id]].add(design)
    strata = {block: "|".join(sorted(parts)) for block, parts in designs.items()}
    return clusters, strata


def _root(parents: dict[str, str], item: str) -> str:
    while parents[item] != item:
        parents[item] = parents[parents[item]]
        item = parents[item]
    return item


def _observations(run: ArenaRun, spec: AnalysisSpec) -> list[Observation]:
    records = {record.job_id: record for record in run.results}
    if len(records) != len(run.results):
        msg = "multiple results for one planned job require explicit retry semantics"
        raise ValueError(msg)
    job_ids = {job.job_id for job in run.plan.jobs}
    if any(job_id not in job_ids for job_id in records):
        msg = "a result does not belong to the saved plan"
        raise ValueError(msg)
    clusters, strata = _clusters(run.plan.jobs)
    started = set(run.status.started_job_ids)
    rows: list[Observation] = []
    for job in run.plan.jobs:
        if not _selected(job, spec):
            continue
        cutoff = spec.cutoff(job.player_count)
        record = records.get(job.job_id)
        for seat, assignment in enumerate(job.seats):
            seat_credits = None
            if record is not None and record.outcome == "finished" and record.scores is not None:
                seat_credits = finish_credits(record.scores, seat, cutoff)
            rows.append(
                Observation(
                    job=job,
                    record=record,
                    config_id=assignment.config_id,
                    seat=seat,
                    opponents=tuple(sorted(other.config_id for index, other in enumerate(job.seats) if index != seat)),
                    block=clusters[job.job_id],
                    stratum=strata[clusters[job.job_id]],
                    started=job.job_id in started,
                    credits=seat_credits,
                )
            )
    return rows


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
    rows: list[Observation], objective: str, universe: list[Observation] | None = None
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
    rows: list[Observation], spec: AnalysisSpec, universe: list[Observation] | None = None, **context: object
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
        estimate = Estimate.model_validate(
            dict(
                context,
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
        )
        estimates.append(estimate)
    return estimates[0], estimates[1]


def _profile(rows: list[Observation], **context: object) -> OutcomeProfile:
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
    return OutcomeProfile.model_validate(
        dict(
            context,
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
    )


def _groups(rows: list[Observation], kind: str) -> dict[tuple, list[Observation]]:
    groups: dict[tuple, list[Observation]] = defaultdict(list)
    for row in rows:
        common = (row.config_id, row.job.player_count)
        if kind == "iid":
            if row.job.stream == "iid":
                groups[(*common, row.job.population_id)].append(row)
        elif kind == "composition":
            groups[(*common, row.opponents)].append(row)
        elif kind == "copies":
            groups[(*common, row.opponents.count(row.config_id) + 1)].append(row)
        elif kind == "seats":
            groups[(*common, row.job.condition_id, row.seat, row.job.permutation)].append(row)
        elif kind == "fixed" and row.job.stream == "fixed":
            groups[(*common, row.job.condition_id, row.seat)].append(row)
    return groups


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
            Estimate.model_validate({
                "estimate_id": identity,
                "view": "weighted",
                "configuration_id": config_id,
                "player_count": n,
                "objective": objective,
                "population_id": population.population_id,
                "value": value if supported else None,
                "interval": interval,
                "status": "unsupported"
                if not supported
                else "estimated"
                if interval is not None
                else "insufficient_data",
                "coverage": coverage,
                "missing_outcome_bounds": (lower, upper) if population_support.target_cell_count > 0 else None,
                "evidence_label": spec.evidence_label,
                "cells": support,
                "target_cell_count": population_support.target_cell_count,
                "unlisted_cell_count": population_support.unlisted_cell_count,
                "unlisted_cell_weight": population_support.unlisted_cell_weight,
                "notes": ("Declared population weights are retained; no missing cells are filled or renormalised.",),
            })
        )
    return estimates[0], estimates[1]


def _view_estimates(
    rows: list[Observation], spec: AnalysisSpec, universe: list[Observation], config_ids: list[str]
) -> tuple[list[Estimate], ...]:
    population: list[Estimate] = []
    composition: list[Estimate] = []
    copies: list[Estimate] = []
    seats: list[Estimate] = []
    iid_groups = _groups(rows, "iid")
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
    for (config_id, n, condition_id, seat), group in _groups(rows, "fixed").items():
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
    for (config_id, n, opponents), group in _groups(rows, "composition").items():
        composition.extend(
            _estimate(group, spec, view="composition", configuration_id=config_id, player_count=n, opponents=opponents)
        )
    for (config_id, n, count), group in _groups(rows, "copies").items():
        copies.extend(
            _estimate(group, spec, view="copy_count", configuration_id=config_id, player_count=n, copy_count=count)
        )
    for (config_id, n, condition_id, seat, permutation), group in _groups(rows, "seats").items():
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
    return population, composition, copies, seats


def _profiles(rows: list[Observation]) -> tuple[OutcomeProfile, ...]:
    profiles: list[OutcomeProfile] = []
    for (config_id, n, population_id), group in _groups(rows, "iid").items():
        profiles.append(
            _profile(group, configuration_id=config_id, player_count=n, view="iid", population_id=population_id)
        )
    for (config_id, n, opponents), group in _groups(rows, "composition").items():
        profiles.append(
            _profile(group, configuration_id=config_id, player_count=n, view="composition", opponents=opponents)
        )
    return tuple(profiles)


def _weakest(estimates: list[Estimate]) -> tuple[Estimate, ...]:
    groups: dict[tuple[str, int, str], list[Estimate]] = defaultdict(list)
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
    spec = analysis_spec if analysis_spec is not None else run.plan.analysis
    universe = _observations(run, spec)
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
        run_status=str(run.status.state),
        artifact_dir=str(run.artifact_dir) if run.artifact_dir is not None else None,
        catalogue={entry.config_id: entry.label for entry in run.plan.catalogue},
        context={
            "rules": run.plan.rules.model_dump(mode="json"),
            "protocol": run.plan.protocol.model_dump(mode="json"),
            "populations": [population.model_dump(mode="json") for population in run.plan.populations],
            "player_counts": sorted({row.job.player_count for row in rows}),
            "streams": sorted({row.job.stream for row in rows}),
        },
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
