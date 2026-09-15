"""Completion and actual resource samples, separate from competitive scores."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from sixnimmt.analytics.models import Diagnostics
from sixnimmt.analytics.uncertainty import quantile

if TYPE_CHECKING:
    from sixnimmt.analytics.evaluation import Observation
    from sixnimmt.arena.records import ArenaRun


def analyse_diagnostics(run: ArenaRun, rows: list[Observation]) -> Diagnostics:
    jobs = {row.job.job_id: row.job for row in rows}
    results = [record for record in run.results if record.job_id in jobs]
    started = set(run.status.started_job_ids) & jobs.keys()
    outcomes = Counter(str(record.outcome) for record in results)
    reasons = Counter(record.reason for record in results if record.outcome != "finished" and record.reason is not None)
    responsible = Counter(
        record.seats[record.ended_by].config_id
        for record in results
        if record.outcome != "finished" and record.ended_by is not None
    )
    samples = [sample for record in results for seat_samples in record.seat_decision_samples for sample in seat_samples]
    totals = [seconds for record in results for seconds in record.seat_decision_seconds if seconds is not None]
    durations = [record.duration_seconds for record in results if record.duration_seconds is not None]
    blocks: dict[str, set[str]] = {}
    for row in rows:
        blocks.setdefault(row.block, set()).add(row.job.job_id)
    finished_ids = {record.job_id for record in results if record.outcome == "finished"}
    return Diagnostics(
        planned_matches=len(jobs),
        started_matches=len(started),
        returned_matches=len(results),
        finished_matches=outcomes.get("finished", 0),
        unsuccessful_matches=sum(count for outcome, count in outcomes.items() if outcome != "finished"),
        missing_matches=len(jobs) - len(results),
        independent_blocks=len(blocks),
        completed_blocks=sum(job_ids <= finished_ids for job_ids in blocks.values()),
        outcomes=dict(outcomes),
        failure_reasons=dict(reasons),
        responsible_configurations=dict(responsible),
        actions_accepted=sum(record.actions_accepted for record in results),
        actions_rejected=sum(record.actions_rejected for record in results),
        measured_decision_calls=sum(
            sum(record.seat_decision_calls)
            if len(record.seat_decision_calls) > 0
            else sum(len(values) for values in record.seat_decision_samples)
            for record in results
        ),
        decision_seconds_total=sum(totals) if len(totals) > 0 else None,
        decision_seconds_median=quantile(samples, 0.5) if len(samples) > 0 else None,
        decision_seconds_p95=quantile(samples, 0.95) if len(samples) > 0 else None,
        match_seconds_total=sum(durations) if len(durations) > 0 else None,
        resource_notes=(
            "Decision counts and total time are always retained; individual call samples and quantiles require tracing. Atomic batches count once per call. Unavailable measurements stay missing.",
            "Token, cost and memory use are not inferred from timing; provider-specific measurements remain in compact seat_stats.",
        ),
    )
