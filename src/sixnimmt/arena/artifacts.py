"""Typed arena artifacts and provenance, independent of match execution."""

import gzip
import hashlib
import json
import os
import platform
import sys
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

from sixnimmt.arena.config import RunConfig, SessionOptions
from sixnimmt.arena.planning import ArenaPlan, PlannedMatch, SeatAssignment
from sixnimmt.arena.results import MatchOutcome
from sixnimmt.common.evaluation import NonNegativeCount, VersionOne
from sixnimmt.persistence.arena import ArenaArtifactWriter
from sixnimmt.persistence.atomic import write_bytes
from sixnimmt.persistence.manifest import ManifestMatch
from sixnimmt.persistence.sink import read_jsonl

NonNegativeSeconds = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class MatchRecord(BaseModel):
    """Seat-aligned measurements; unfinished matches have no competitive scores."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    record_version: VersionOne = 1
    job_id: str
    match_id: str
    seats: tuple[SeatAssignment, ...]
    outcome: MatchOutcome
    scores: tuple[NonNegativeCount, ...] | None = None
    winners: tuple[NonNegativeCount, ...] = ()
    completed_hand_scores: tuple[tuple[NonNegativeCount, ...], ...] = ()
    partial_scores: tuple[NonNegativeCount, ...] | None = None
    ended_by: NonNegativeCount | None = None
    reason: str | None = None
    actions_accepted: NonNegativeCount = 0
    actions_rejected: NonNegativeCount = 0
    seat_actions: tuple[tuple[NonNegativeCount, NonNegativeCount], ...] = ()
    duration_seconds: NonNegativeSeconds | None = None
    seat_decision_seconds: tuple[NonNegativeSeconds | None, ...] = ()
    seat_decision_calls: tuple[NonNegativeCount, ...] = ()
    # Detailed timings are retained only for traced runs; totals and counts are always collected.
    seat_decision_samples: tuple[tuple[NonNegativeSeconds, ...], ...] = ()
    seat_stats: tuple[dict[str, JsonValue] | None, ...] = ()
    stats_errors: dict[str, str] = Field(default_factory=dict)
    event_trace: str | None = None
    action_trace: str | None = None

    @property
    def completed_hands(self) -> int:
        return len(self.completed_hand_scores)

    @model_validator(mode="after")
    def _validate_scores(self) -> "MatchRecord":
        count = len(self.seats)
        if self.outcome != MatchOutcome.FINISHED:
            if self.scores is not None or len(self.winners) > 0:
                msg = "unfinished outcomes cannot carry competitive scores or winners"
                raise ValueError(msg)
        elif self.scores is None or len(self.scores) != count:
            msg = "finished outcomes require one score per seat"
            raise ValueError(msg)
        if self.scores is not None:
            lowest = min(self.scores)
            expected = tuple(index for index, score in enumerate(self.scores) if score == lowest)
            if self.winners != expected:
                msg = "winners must identify all lowest-score seats"
                raise ValueError(msg)
        if any(len(scores) != count for scores in self.completed_hand_scores):
            msg = "completed hands must have one score per seat"
            raise ValueError(msg)
        if self.partial_scores is not None and len(self.partial_scores) != count:
            msg = "partial scores must have one value per seat"
            raise ValueError(msg)
        if self.ended_by is not None and not 0 <= self.ended_by < count:
            msg = "responsible seat is outside the lineup"
            raise ValueError(msg)
        self._validate_measurements(count)
        return self

    def _validate_measurements(self, count: int) -> None:
        for values in (
            self.seat_actions,
            self.seat_decision_seconds,
            self.seat_decision_calls,
            self.seat_decision_samples,
            self.seat_stats,
        ):
            if len(values) not in (0, count):
                msg = "seat measurements must align with the lineup"
                raise ValueError(msg)


class RunStatus(BaseModel):
    """Started means submitted to a worker; lost jobs have no returned outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    status_version: VersionOne = 1
    state: Literal["running", "completed", "stopped", "failed"]
    planned_job_ids: tuple[str, ...]
    started_job_ids: tuple[str, ...] = ()
    completed_job_ids: tuple[str, ...] = ()
    lost_job_ids: tuple[str, ...] = ()
    unstarted_job_ids: tuple[str, ...] = ()
    decisions_abandoned: NonNegativeCount = 0
    error: str | None = None


_VERSION_ONE = TypeAdapter(VersionOne)


def _validate_provenance_version(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    # Older recordings may have no version tag or newer runtime detail fields.
    # Preserve their extensible payload while validating any declared version.
    if "provenance_version" in value:
        _VERSION_ONE.validate_python(value["provenance_version"])
    return value


RecordedProvenance = Annotated[dict[str, JsonValue], AfterValidator(_validate_provenance_version)]


class ArenaRun(BaseModel):
    """A reloadable plan, its returned outcomes, and execution provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    plan: ArenaPlan
    results: tuple[MatchRecord, ...]
    status: RunStatus
    artifact_dir: Path | None = None
    provenance: RecordedProvenance = Field(default_factory=dict)


def validate_run_evidence(plan: ArenaPlan, results: tuple[MatchRecord, ...], status: RunStatus) -> None:
    """Validate factual identities before loading or analysing a run.

    Committed results may be newer than the last status snapshot. They need not
    already occur in its started/completed lists, but cannot disagree with jobs.
    """
    jobs = {job.job_id: job for job in plan.jobs}
    if status.planned_job_ids != tuple(jobs):
        msg = "execution status does not match the saved plan"
        raise ValueError(msg)
    for identifiers in (
        status.started_job_ids,
        status.completed_job_ids,
        status.lost_job_ids,
        status.unstarted_job_ids,
    ):
        if len(set(identifiers)) != len(identifiers) or any(job_id not in jobs for job_id in identifiers):
            msg = "execution status contains an unknown or duplicate job"
            raise ValueError(msg)
    if not set(status.started_job_ids).isdisjoint(status.unstarted_job_ids):
        msg = "execution status marks a job both started and unstarted"
        raise ValueError(msg)
    if not set(status.completed_job_ids).isdisjoint(status.lost_job_ids):
        msg = "execution status marks a job both completed and lost"
        raise ValueError(msg)
    seen: set[str] = set()
    for result in results:
        if result.job_id not in jobs or result.job_id in seen:
            msg = "results contain an unknown or duplicate job"
            raise ValueError(msg)
        seen.add(result.job_id)
        job = jobs[result.job_id]
        if result.match_id != job.match_id or result.seats != job.seats:
            msg = "result identities do not match the planned job"
            raise ValueError(msg)
    if not set(status.completed_job_ids) <= seen:
        msg = "execution status reports completion without a committed result"
        raise ValueError(msg)


class RuntimeProvenance(BaseModel):
    """Machine, source, and execution facts recorded at the start of a run."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    provenance_version: VersionOne = 1
    python: str
    platform: str
    machine: str
    processor: str
    cpu_count: int | None
    package_source_sha256: str
    file_sha256: dict[str, str | None]
    dependencies: dict[str, str]
    seed_scheme_version: str
    execution: RunConfig
    trace_enabled: bool
    session: SessionOptions
    trace_directory: str | None


class _ArenaManifest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    manifest_version: VersionOne
    artifact_kind: Literal["arena_run"]
    plan_id: str
    status: RunStatus
    provenance: RecordedProvenance


_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def runtime_provenance(
    plan: ArenaPlan, *, trace: bool = False, session: SessionOptions | None = None
) -> dict[str, JsonValue]:
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
    provenance = RuntimeProvenance(
        python=sys.version,
        platform=platform.platform(),
        machine=platform.machine(),
        processor=platform.processor(),
        cpu_count=os.cpu_count(),
        package_source_sha256=source.hexdigest(),
        file_sha256=hashes,
        dependencies=dict(sorted(versions.items())),
        seed_scheme_version=plan.seed_scheme_version,
        execution=plan.execution,
        trace_enabled=trace,
        session=SessionOptions() if session is None else session,
        trace_directory="traces" if trace else None,
    )
    return _JSON_OBJECT.validate_python(provenance.model_dump(mode="json"))


class ArenaRunWriter(ArenaArtifactWriter):
    """Publish the complete schedule before workers can construct any bots."""

    def __init__(
        self,
        directory: Path,
        plan: ArenaPlan,
        status: RunStatus,
        *,
        trace: bool = False,
        session: SessionOptions | None = None,
    ) -> None:
        self._trace_enabled = trace
        self._trace_entries: list[ManifestMatch] = []
        self._jobs = {job.job_id: (index, job) for index, job in enumerate(plan.jobs)}
        super().__init__(
            directory,
            plan.model_dump(mode="json"),
            status.model_dump(mode="json"),
            runtime_provenance(plan, trace=trace, session=session),
        )
        if trace:
            try:
                self._write_trace_manifest()
            except BaseException:
                self.close()
                raise

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
    results = tuple(read_jsonl(directory / "results.jsonl", MatchRecord.model_validate_json))
    manifest = _ArenaManifest.model_validate_json((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.plan_id != plan.plan_id:
        msg = "arena manifest does not match the saved plan"
        raise ValueError(msg)
    status = manifest.status
    validate_run_evidence(plan, results, status)
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
        provenance=manifest.provenance,
    )


def _validate_legacy_jobs(directory: Path, plan: ArenaPlan) -> None:
    path = directory / "jobs.jsonl"
    if not path.exists():
        return
    jobs = tuple(read_jsonl(path, PlannedMatch.model_validate_json))
    if jobs != plan.jobs:
        msg = "planned-job records do not match the saved plan"
        raise ValueError(msg)


@dataclass(frozen=True)
class AnalysisArtifactPaths:
    analysis: Path
    report: Path


def publish_analysis(directory: Path, analysis_payload: object, report_text: str) -> AnalysisArtifactPaths:
    """Atomically publish derived reports and return only the paths actually written."""
    analysis = directory / "analysis.json.gz"
    report = directory / "report.md"
    payload = json.dumps(analysis_payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
    write_bytes(analysis, gzip.compress(payload, mtime=0), durable=False)
    write_bytes(report, report_text.encode("utf-8"), durable=False)
    return AnalysisArtifactPaths(analysis.resolve(), report.resolve())
