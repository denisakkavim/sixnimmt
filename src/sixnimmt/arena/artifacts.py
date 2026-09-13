"""Typed arena artifacts and provenance, independent of match execution."""

import hashlib
import json
import os
import platform
import sys
from dataclasses import asdict
from importlib.metadata import distributions
from pathlib import Path
from typing import Any

from sixnimmt.arena.planning import ArenaPlan, PlannedMatch
from sixnimmt.arena.records import ArenaRun, MatchRecord, RunStatus
from sixnimmt.persistence.arena import ArenaArtifactWriter
from sixnimmt.persistence.manifest import ManifestMatch
from sixnimmt.persistence.sink import _parse_log


def runtime_provenance(plan: ArenaPlan, *, trace: bool = False) -> dict[str, Any]:
    """Record installed dependencies and source content, including local edits."""
    package = Path(__file__).resolve().parents[1]
    source = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        source.update(str(path.relative_to(package)).encode("utf-8"))
        source.update(path.read_bytes())
    root = package.parent.parent
    hashes = {}
    for name in ("uv.lock", "pyproject.toml"):
        path = root / name
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    versions = {}
    for distribution in distributions():
        name = distribution.metadata.get("Name")
        if name is not None:
            versions[name] = distribution.version
    settings = asdict(plan.execution)
    return {
        "provenance_version": 1,
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "package_source_sha256": source.hexdigest(),
        "file_sha256": hashes,
        "dependencies": dict(sorted(versions.items())),
        "seed_scheme_version": plan.seed_scheme_version,
        "execution": settings,
        "trace_enabled": trace,
        "trace_directory": "traces" if trace else None,
    }


class ArenaRunWriter(ArenaArtifactWriter):
    """Publish the complete schedule before workers can construct any bots."""

    def __init__(self, directory: Path, plan: ArenaPlan, status: RunStatus, *, trace: bool = False) -> None:
        self._trace_enabled = trace
        self._trace_entries: list[ManifestMatch] = []
        self._jobs = {job.job_id: (index, job) for index, job in enumerate(plan.jobs)}
        super().__init__(
            directory,
            plan.model_dump(mode="json"),
            status.model_dump(mode="json"),
            runtime_provenance(plan, trace=trace),
        )
        if trace:
            self._write_trace_manifest()

    def append(self, result: MatchRecord) -> None:
        payload = result.model_dump(mode="json", exclude_defaults=True)
        payload["record_version"] = result.record_version
        self.append_payload(payload)
        if not self._trace_enabled:
            return
        index, job = self._jobs[result.job_id]
        entry = ManifestMatch(
            game_index=index,
            match_id=result.match_id,
            seed=job.match_seed,
            outcome=result.outcome.value,
            winners=tuple(f"player_{seat + 1}" for seat in result.winners),
            ended_by=f"player_{result.ended_by + 1}" if result.ended_by is not None else None,
            reason=result.reason,
            log=f"{result.match_id}.jsonl",
            actions=f"{result.match_id}.actions.jsonl",
            seat_stats={f"player_{seat + 1}": stats for seat, stats in enumerate(result.seat_stats)},
            stats_errors=result.stats_errors,
        )
        self._trace_entries.append(entry)
        self._write_trace_manifest()

    def _write_trace_manifest(self) -> None:
        self.write_trace_manifest({
            "manifest_version": 1,
            "run_id": self.plan["plan_id"],
            "arena_manifest": "../manifest.json",
            "matches": [entry.model_dump(mode="json") for entry in self._trace_entries],
        })

    def write_status(self, status: RunStatus) -> None:
        self.write_status_payload(status.model_dump(mode="json"))


def load_run(directory: Path | str) -> ArenaRun:
    """Read saved outcomes without importing registry entries or constructing bots.

    Results are authoritative if a process stopped between appending a result
    and refreshing status. Pending JSONL batches are excluded by the log reader.
    """
    directory = Path(directory).resolve()
    plan = ArenaPlan.model_validate_json((directory / "plan.json").read_text(encoding="utf-8"))
    _validate_legacy_jobs(directory, plan)
    results = tuple(_parse_log(directory / "results.jsonl", MatchRecord.model_validate_json))
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != 1 or manifest.get("artifact_kind") != "arena_run":
        msg = "unsupported arena manifest"
        raise ValueError(msg)
    if manifest["plan_id"] != plan.plan_id:
        msg = "arena manifest does not match the saved plan"
        raise ValueError(msg)
    status = RunStatus.model_validate(manifest["status"])
    _validate_records(plan, results, status)
    by_id = {result.job_id: result for result in results}
    returned = set(by_id)
    started = set(status.started_job_ids) | returned
    ordered = tuple(job.job_id for job in plan.jobs)
    status = status.model_copy(
        update={
            "completed_job_ids": tuple(job_id for job_id in ordered if job_id in returned),
            "started_job_ids": tuple(job_id for job_id in ordered if job_id in started),
            "lost_job_ids": tuple(job_id for job_id in ordered if job_id in started and job_id not in returned),
            "unstarted_job_ids": tuple(job_id for job_id in ordered if job_id not in started),
        }
    )
    return ArenaRun(
        plan=plan,
        results=tuple(by_id[job_id] for job_id in ordered if job_id in by_id),
        status=status,
        artifact_dir=directory,
        provenance=manifest["provenance"],
    )


def _validate_legacy_jobs(directory: Path, plan: ArenaPlan) -> None:
    path = directory / "jobs.jsonl"
    if not path.exists():
        return
    jobs = tuple(_parse_log(path, PlannedMatch.model_validate_json))
    if jobs != plan.jobs:
        msg = "planned-job records do not match the saved plan"
        raise ValueError(msg)


def _validate_records(plan: ArenaPlan, results: tuple[MatchRecord, ...], status: RunStatus) -> None:
    jobs = {job.job_id: job for job in plan.jobs}
    if status.planned_job_ids != tuple(jobs):
        msg = "execution status does not match the saved plan"
        raise ValueError(msg)
    if any(job_id not in jobs for job_id in status.started_job_ids):
        msg = "execution status contains an unknown job"
        raise ValueError(msg)
    seen = set()
    for result in results:
        if result.job_id not in jobs or result.job_id in seen:
            msg = "results contain an unknown or duplicate job"
            raise ValueError(msg)
        seen.add(result.job_id)
        job = jobs[result.job_id]
        if result.match_id != job.match_id or result.seats != job.seats:
            msg = "result identities do not match the planned job"
            raise ValueError(msg)
