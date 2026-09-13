"""Prespecified focal replacements, averaged within complete dependent blocks."""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from typing import TYPE_CHECKING

from sixnimmt.analytics.models import AnalysisSpec, CellSupport, Coverage, Estimate
from sixnimmt.analytics.uncertainty import ratio_interval, weighted_interval

if TYPE_CHECKING:
    from sixnimmt.analytics.evaluation import Observation
    from sixnimmt.arena.planning import ArenaPlan, Population


def _paired_blocks(
    rows: list[Observation], candidate: str, reference: str, objective: str
) -> tuple[dict[str, tuple[str, float, int]], int]:
    by_block: dict[str, dict[str, list[Observation]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if row.job.arm_id in (candidate, reference):
            by_block[row.block][row.job.arm_id].append(row)
    effects: dict[str, tuple[str, float, int]] = {}
    for block, arms in by_block.items():
        candidate_rows = arms[candidate]
        reference_rows = arms[reference]
        if not _complete_pair(candidate_rows, reference_rows):
            continue
        candidate_values = [row.value(objective) for row in candidate_rows]
        reference_values = [row.value(objective) for row in reference_rows]
        candidate_mean = sum(value for value in candidate_values if value is not None) / len(candidate_values)
        reference_mean = sum(value for value in reference_values if value is not None) / len(reference_values)
        effects[block] = (candidate_rows[0].stratum, candidate_mean - reference_mean, 1)
    return effects, len(by_block)


def _complete_pair(candidate: list[Observation], reference: list[Observation]) -> bool:
    if len(candidate) == 0 or len(reference) == 0:
        return False
    if any(row.credits is None for row in (*candidate, *reference)):
        return False
    candidate_positions = {(row.job.shared_deal_id, row.job.rotation, row.job.permutation) for row in candidate}
    reference_positions = {(row.job.shared_deal_id, row.job.rotation, row.job.permutation) for row in reference}
    return candidate_positions == reference_positions and len(candidate) == len(reference)


def _interpretation(estimate: Estimate, spec: AnalysisSpec) -> str:
    interval = estimate.interval
    if interval is None:
        return "unresolved"
    threshold = spec.practical_effect_threshold
    if interval.low > threshold:
        return "useful gain"
    if interval.high < -threshold:
        return "useful loss"
    if interval.low >= -threshold and interval.high <= threshold:
        return "practical equivalence"
    return "unresolved"


def _comparison(
    rows: list[Observation], candidate: str, reference: str, spec: AnalysisSpec, condition: str | None
) -> tuple[list[Estimate], dict[str, dict[str, tuple[str, float, int]]]]:
    estimates: list[Estimate] = []
    objective_blocks: dict[str, dict[str, tuple[str, float, int]]] = {}
    for objective in ("win_credit", "acceptable_credit"):
        blocks, planned_blocks = _paired_blocks(rows, candidate, reference, objective)
        objective_blocks[objective] = blocks
        first = rows[0].job
        identity = f"paired:{first.comparison_id}:{candidate}:{reference}:{first.player_count}:{condition}:{objective}"
        interval = ratio_interval(blocks, spec, identity)
        total = sum(value for _, value, _ in blocks.values())
        missing = planned_blocks - len(blocks)
        coverage = Coverage(
            planned_appearances=len(rows),
            started_appearances=sum(row.started for row in rows),
            finished_appearances=sum(row.credits is not None for row in rows),
            planned_blocks=planned_blocks,
            finished_blocks=len(blocks),
            complete_blocks=len(blocks),
            incomplete_pairs=missing,
            completion_fraction=len(blocks) / planned_blocks if planned_blocks > 0 else None,
        )
        estimate = Estimate.model_validate({
            "estimate_id": identity,
            "view": "paired_replacement",
            "configuration_id": candidate,
            "reference_id": reference,
            "comparison_id": first.comparison_id,
            "population_id": first.population_id,
            "player_count": first.player_count,
            "condition_id": condition,
            "stream": "matched",
            "objective": objective,
            "value": total / len(blocks) if len(blocks) > 0 else None,
            "interval": interval,
            "status": "estimated" if interval is not None else "insufficient_data",
            "coverage": coverage,
            "missing_outcome_bounds": ((total - missing) / planned_blocks, (total + missing) / planned_blocks)
            if planned_blocks > 0
            else None,
            "evidence_label": spec.evidence_label,
            "notes": (
                "Candidate minus reference. Each independent sample contributes the average difference between its completed candidate and reference games.",
            ),
        })
        estimates.append(estimate.model_copy(update={"interpretation": _interpretation(estimate, spec)}))
    return estimates, objective_blocks


def _weighted_comparison(
    rows: list[Observation], candidate: str, reference: str, population: Population, spec: AnalysisSpec
) -> list[Estimate]:
    base, _ = _comparison(rows, candidate, reference, spec, None)
    n = rows[0].job.player_count
    weights = {
        tuple(sorted(cell.opponents)): cell.weight for cell in population.composition_weights if cell.player_count == n
    }
    cell_rows = {opponents: [row for row in rows if row.opponents == opponents] for opponents in weights}
    estimates: list[Estimate] = []
    for template in base:
        blocks: dict[str, tuple[str, dict[tuple[str, ...], tuple[float, int]]]] = {
            row.block: (row.stratum, {}) for row in rows
        }
        support: list[CellSupport] = []
        point = 0.0
        low = 0.0
        high = 0.0
        supported = len(weights) > 0
        for opponents, weight in weights.items():
            source = cell_rows[opponents]
            effects, planned = _paired_blocks(source, candidate, reference, template.objective)
            finished = len(effects)
            total = sum(value for _, value, _ in effects.values())
            support.append(
                CellSupport(
                    opponents=opponents,
                    weight=weight,
                    finished_appearances=sum(row.credits is not None for row in source),
                    planned_appearances=len(source),
                    finished_blocks=finished,
                )
            )
            if finished == 0:
                supported = False
            else:
                point += weight * total / finished
            low += weight * (total - planned + finished) / planned if planned > 0 else -weight
            high += weight * (total + planned - finished) / planned if planned > 0 else weight
            for block, (stratum, value, denominator) in effects.items():
                if block not in blocks:
                    blocks[block] = (stratum, {})
                blocks[block][1][opponents] = (value, denominator)
        interval = weighted_interval(blocks, weights, spec, template.estimate_id) if supported else None
        estimate = template.model_copy(
            update={
                "value": point if supported else None,
                "interval": interval,
                "status": "unsupported"
                if not supported
                else "estimated"
                if interval is not None
                else "insufficient_data",
                "missing_outcome_bounds": (low, high),
                "cells": tuple(support),
                "notes": (
                    "Candidate minus reference. Effects from completed comparisons are averaged within each declared opponent combination, then combined with its frozen population weight.",
                ),
            }
        )
        estimates.append(estimate.model_copy(update={"interpretation": _interpretation(estimate, spec)}))
    return estimates


def _condition_differences(
    conditions: dict[str, dict[str, dict[str, tuple[str, float, int]]]],
    template: Estimate,
    spec: AnalysisSpec,
    planned_conditions: dict[str, set[str]],
    started_conditions: dict[str, set[str]],
) -> list[Estimate]:
    differences: list[Estimate] = []
    for left, right in combinations(conditions, 2):
        for objective in ("win_credit", "acceptable_credit"):
            left_blocks = conditions[left][objective]
            right_blocks = conditions[right][objective]
            common = left_blocks.keys() & right_blocks.keys()
            declared = planned_conditions[left] & planned_conditions[right]
            if len(declared) == 0:
                continue
            blocks = {
                block: (left_blocks[block][0], right_blocks[block][1] - left_blocks[block][1], 1) for block in common
            }
            identity = f"condition-difference:{template.estimate_id}:{left}:{right}:{objective}"
            interval = ratio_interval(blocks, spec, identity)
            planned = len(declared)
            coverage = Coverage(
                planned_appearances=planned,
                started_appearances=len(declared & started_conditions[left] & started_conditions[right]),
                finished_appearances=len(common),
                planned_blocks=planned,
                finished_blocks=len(common),
                complete_blocks=len(common),
                incomplete_pairs=planned - len(common),
                completion_fraction=len(common) / planned,
            )
            differences.append(
                template.model_copy(
                    update={
                        "estimate_id": identity,
                        "view": "paired_condition_difference",
                        "condition_id": f"{right} minus {left}",
                        "objective": objective,
                        "value": sum(value for _, value, _ in blocks.values()) / len(blocks)
                        if len(blocks) > 0
                        else None,
                        "interval": interval,
                        "status": "estimated" if interval is not None else "insufficient_data",
                        "coverage": coverage,
                        "missing_outcome_bounds": (
                            (sum(value for _, value, _ in blocks.values()) - 2 * (planned - len(common))) / planned,
                            (sum(value for _, value, _ in blocks.values()) + 2 * (planned - len(common))) / planned,
                        ),
                        "evidence_label": "exploratory",
                        "interpretation": None,
                        "cells": (),
                        "notes": (
                            "Change in candidate-minus-reference performance, using only deals shared between both conditions.",
                        ),
                    }
                )
            )
    return differences


def _fully_started_blocks(rows: list[Observation]) -> set[str]:
    blocks: dict[str, list[Observation]] = defaultdict(list)
    for row in rows:
        blocks[row.block].append(row)
    return {block for block, group in blocks.items() if all(row.started for row in group)}


def comparison_estimates(
    rows: list[Observation], plan: ArenaPlan, spec: AnalysisSpec
) -> tuple[list[Estimate], list[Estimate]]:
    estimates: list[Estimate] = []
    differences: list[Estimate] = []
    for comparison in plan.comparisons:
        for candidate in comparison.candidates:
            groups: dict[int, list[Observation]] = defaultdict(list)
            for row in rows:
                if (
                    row.job.comparison_id == comparison.comparison_id
                    and row.seat == row.job.focal_seat
                    and row.job.arm_id in (candidate, comparison.reference)
                ):
                    groups[row.job.player_count].append(row)
            for group in groups.values():
                population = next(item for item in plan.populations if item.population_id == comparison.population_id)
                if population.kind == "subsets":
                    overall = _weighted_comparison(group, candidate, comparison.reference, population, spec)
                else:
                    overall, _ = _comparison(group, candidate, comparison.reference, spec, None)
                estimates.extend(overall)
                by_condition: dict[str, list[Observation]] = defaultdict(list)
                for row in group:
                    by_condition[row.job.condition_id].append(row)
                condition_blocks: dict[str, dict[str, dict[str, tuple[str, float, int]]]] = {}
                for condition, condition_rows in by_condition.items():
                    conditional, blocks = _comparison(condition_rows, candidate, comparison.reference, spec, condition)
                    estimates.extend(conditional)
                    condition_blocks[condition] = blocks
                differences.extend(
                    _condition_differences(
                        condition_blocks,
                        overall[0],
                        spec,
                        {
                            condition: {row.block for row in condition_rows}
                            for condition, condition_rows in by_condition.items()
                        },
                        {
                            condition: _fully_started_blocks(condition_rows)
                            for condition, condition_rows in by_condition.items()
                        },
                    )
                )
    return estimates, differences
