"""Validated evidence observations and their independent sampling groups."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sixnimmt.analytics.metrics import FinishCredits, finish_credits
from sixnimmt.arena.artifacts import ArenaRun, MatchRecord
from sixnimmt.arena.planning import PlannedMatch
from sixnimmt.common.evaluation import AnalysisSpec, Objective


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

    def value(self, objective: Objective) -> float | None:
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


def observations(run: ArenaRun, spec: AnalysisSpec) -> list[Observation]:
    records = {record.job_id: record for record in run.results}
    clusters, strata = _clusters(run.plan.jobs)
    started = set(run.status.started_job_ids) | set(records)
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
