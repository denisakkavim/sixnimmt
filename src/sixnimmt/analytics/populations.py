"""Declared opponent probabilities with bounded descriptions of unplayed combinations."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from itertools import combinations_with_replacement
from math import comb, factorial, fsum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sixnimmt.arena.planning import Population


@dataclass(frozen=True)
class PopulationSupport:
    cell_weights: dict[tuple[str, ...], float]
    target_cell_count: int
    unlisted_cell_count: int = 0
    unlisted_cell_weight: float = 0.0


def population_weights(
    population: Population, player_count: int, observed_compositions: set[tuple[str, ...]]
) -> PopulationSupport:
    """List small populations fully and retain the exact unlisted probability for large ones."""
    if population.kind == "subsets":
        weights = {
            tuple(sorted(cell.opponents)): cell.weight
            for cell in population.composition_weights
            if cell.player_count == player_count
        }
        return PopulationSupport(weights, len(weights))
    probabilities = {entry.config_id: entry.weight for entry in population.weights}
    target_cell_count = comb(len(probabilities) + player_count - 2, player_count - 1)
    # The named entry probabilities already define every possible combination.
    # Materialising thousands of unplayed combinations adds no outcome evidence.
    if target_cell_count <= 1024:
        compositions = combinations_with_replacement(sorted(probabilities), player_count - 1)
    else:
        compositions = iter(
            sorted(
                opponents
                for opponents in observed_compositions
                if len(opponents) == player_count - 1 and set(opponents) <= probabilities.keys()
            )
        )
    weights = {opponents: _composition_weight(opponents, probabilities) for opponents in compositions}
    unlisted = target_cell_count - len(weights)
    unlisted_weight = max(0.0, 1 - fsum(weights.values())) if unlisted > 0 else 0.0
    return PopulationSupport(weights, target_cell_count, unlisted, unlisted_weight)


def _composition_weight(opponents: tuple[str, ...], probabilities: dict[str, float]) -> float:
    weight = float(factorial(len(opponents)))
    for config_id, copies in Counter(opponents).items():
        weight *= probabilities[config_id] ** copies / factorial(copies)
    return weight
