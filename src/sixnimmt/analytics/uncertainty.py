"""Resample dependent observations together without changing controlled quotas."""

import hashlib
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence

from sixnimmt.analytics.models import AnalysisSpec, Interval


def quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def seeded_random(spec: AnalysisSpec, identity: str) -> random.Random:
    digest = hashlib.sha256(f"{spec.resampling_seed}:{identity}".encode()).digest()
    return random.Random(int.from_bytes(digest, "big"))  # noqa: S311 - deterministic statistical resampling


def ratio_interval(blocks: Mapping[str, tuple[str, float, int]], spec: AnalysisSpec, identity: str) -> Interval | None:
    """Sample every design stratum at its observed number of dependent blocks."""
    strata: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for stratum, numerator, denominator in blocks.values():
        strata[stratum].append((numerator, denominator))
    if spec.bootstrap_samples < 2 or len(strata) == 0 or any(len(blocks) < 2 for blocks in strata.values()):
        return None
    rng = seeded_random(spec, identity)
    samples: list[float] = []
    for _ in range(spec.bootstrap_samples):
        numerator = 0.0
        denominator = 0
        for entries in strata.values():
            for _ in entries:
                block_numerator, block_denominator = rng.choice(entries)
                numerator += block_numerator
                denominator += block_denominator
        if denominator == 0:
            return None
        samples.append(numerator / denominator)
    return samples_interval(samples, spec)


def samples_interval(samples: Sequence[float], spec: AnalysisSpec) -> Interval:
    tail = (1 - spec.confidence_level) / 2
    return Interval(
        low=quantile(samples, tail),
        high=quantile(samples, 1 - tail),
        confidence_level=spec.confidence_level,
    )


def weighted_interval(
    blocks: Mapping[str, tuple[str, dict[tuple[str, ...], tuple[float, int]]]],
    weights: Mapping[tuple[str, ...], float],
    spec: AnalysisSpec,
    identity: str,
) -> Interval | None:
    """Reuse each sampled block's multiplicity for all supported population cells."""
    strata: dict[str, list[dict[tuple[str, ...], tuple[float, int]]]] = defaultdict(list)
    for stratum, cells in blocks.values():
        strata[stratum].append(cells)
    if spec.bootstrap_samples < 2 or len(strata) == 0 or any(len(items) < 2 for items in strata.values()):
        return None
    rng = seeded_random(spec, identity)
    samples: list[float] = []
    for _ in range(spec.bootstrap_samples):
        totals: dict[tuple[str, ...], list[float]] = {cell: [0.0, 0.0] for cell in weights}
        for entries in strata.values():
            for _ in entries:
                for cell, (numerator, denominator) in rng.choice(entries).items():
                    if cell in totals:
                        totals[cell][0] += numerator
                        totals[cell][1] += denominator
        # Discarding unsupported bootstrap draws would condition the interval
        # on observing rare cells; report the missing uncertainty instead.
        if any(denominator == 0 for _, denominator in totals.values()):
            return None
        samples.append(
            sum(weights[cell] * numerator / denominator for cell, (numerator, denominator) in totals.items())
        )
    return samples_interval(samples, spec)
