"""Execute changing lineups with explicit seeds and durable compact results."""

from collections.abc import Callable, Sequence
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import partial
from multiprocessing import get_context
from pathlib import Path
from time import monotonic
from typing import Any

from sixnimmt.arena.artifacts import ArenaRunWriter, runtime_provenance
from sixnimmt.arena.artifacts import load_run as load_run
from sixnimmt.arena.bots.base import Bot
from sixnimmt.arena.catalogue import resolve_frozen_catalogue
from sixnimmt.arena.config import RunConfig, resolved_abandoned_limit
from sixnimmt.arena.decisions import AbandonedDecisions, SharedAbandonedState
from sixnimmt.arena.execution import _collect_work, _validate_process_players
from sixnimmt.arena.match import ArenaError, Observer, _Match, _run_match
from sixnimmt.arena.planning import ArenaPlan, PlannedMatch, with_execution
from sixnimmt.arena.players import ResolvedPlayer
from sixnimmt.arena.records import ArenaRun, MatchRecord, RunStatus
from sixnimmt.arena.results import MatchOutcome, MatchResult
from sixnimmt.arena.tracing import collect_stats
from sixnimmt.engine.events import Event
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.state import PlayerSeat
from sixnimmt.persistence.sink import ActionRecord, EventSink, JsonlEventSink, NullEventSink


class PlannedArenaError(ArenaError):
    """Execution failed; the attached run retains every collected outcome."""

    def __init__(self, message: str, run: ArenaRun) -> None:
        detail = message if run.artifact_dir is None else f"{message}; saved run: {run.artifact_dir}"
        super().__init__(detail)
        self.run = run
        self.artifact_dir = run.artifact_dir


class _MeasuredSink:
    def __init__(self, sink: EventSink, seats: Sequence[PlayerSeat]) -> None:
        self.sink = sink
        self.samples: dict[str, list[float]] = {seat.player_id: [] for seat in seats}

    def append(self, events: Sequence[Event]) -> None:
        self.sink.append(events)

    def record_action(self, record: ActionRecord) -> None:
        self.sink.record_action(record)

    def record_call(self, player_id: str, duration_ms: float) -> None:
        # Calls count even when a batch exceeds an action limit before submission.
        self.samples[player_id].append(duration_ms / 1000)

    def record_model(self, payload: dict[str, Any], *, player_id: str, display_name: str) -> None:
        if isinstance(self.sink, JsonlEventSink):
            self.sink.record_model(payload, player_id=player_id, display_name=display_name)

    def close(self) -> None:
        self.sink.close()


@dataclass(frozen=True)
class _WorkerSettings:
    catalogue: dict[str, ResolvedPlayer]
    rules: GameRules
    protocol: MatchProtocol
    config: RunConfig


_worker_settings: _WorkerSettings | None = None
_worker_abandoned: AbandonedDecisions | None = None


def _initialise_worker(settings: _WorkerSettings, shared: SharedAbandonedState | None) -> None:
    global _worker_settings, _worker_abandoned
    _worker_settings = settings
    _worker_abandoned = AbandonedDecisions(resolved_abandoned_limit(settings.config), shared)


def _play_process_job(job: PlannedMatch) -> MatchRecord:
    if _worker_settings is None or _worker_abandoned is None:
        msg = "planned arena worker was not initialized"
        raise ArenaError(msg)
    return _play_job(_worker_settings, _worker_abandoned, None, job)


def _play_job(
    settings: _WorkerSettings, abandoned: AbandonedDecisions, observer: Observer | None, job: PlannedMatch
) -> MatchRecord:
    # Analysis labels belong to the harness, never to player observations.
    seats = [
        PlayerSeat(player_id=f"player_{seat.seat + 1}", display_name=f"Player {seat.seat + 1}") for seat in job.seats
    ]
    raw_sink = (
        JsonlEventSink(settings.config.trace_dir, job.match_id)
        if settings.config.trace_dir is not None
        else NullEventSink()
    )
    sink = _MeasuredSink(raw_sink, seats)
    bots: list[Bot] = []
    started = monotonic()
    try:
        build_error = None
        for assignment in job.seats:
            try:
                bots.append(settings.catalogue[assignment.config_id].build(assignment.bot_seed))
            except Exception as error:
                build_error = error
                break
        if build_error is not None:
            match = _Match(
                job.match_seed, job.match_id, seats, settings.rules, settings.protocol, settings.config, sink, observer
            )
            result = match.finish(MatchOutcome.FAILED, seats[len(bots)].player_id, repr(build_error))
        else:
            result = _run_match(
                bots,
                job.match_seed,
                match_id=job.match_id,
                rules=settings.rules,
                protocol=settings.protocol,
                config=settings.config,
                seats=seats,
                observer=observer,
                sink=sink,
                _abandoned=abandoned,
            )
        return _record_result(job, result, bots, sink, monotonic() - started, settings)
    finally:
        sink.close()


def _record_result(
    job: PlannedMatch,
    result: MatchResult,
    bots: Sequence[Bot],
    sink: _MeasuredSink,
    duration: float,
    settings: _WorkerSettings,
) -> MatchRecord:
    player_ids = tuple(player.player_id for player in result.final_state.players)
    hand_scores = tuple(
        tuple(event.data["hand_scores"][player_id] for player_id in player_ids)
        for event in result.events
        if event.type == "hand_ended"
    )
    finished = result.outcome == MatchOutcome.FINISHED
    scores = tuple(player.total_score for player in result.final_state.players) if finished else None
    partial_scores = None if finished else tuple(player.score_this_hand for player in result.final_state.players)
    stats, errors = collect_stats(bots, result.ended_by if result.reason == "decision_timeout" else None)
    samples = tuple(tuple(sink.samples[player_id]) for player_id in player_ids)
    trace_dir = settings.config.trace_dir
    event_trace = None
    action_trace = None
    if trace_dir is not None:
        event_trace = f"traces/{job.match_id}.jsonl"
        action_trace = f"traces/{job.match_id}.actions.jsonl"
    return MatchRecord(
        job_id=job.job_id,
        match_id=job.match_id,
        seats=job.seats,
        outcome=result.outcome,
        scores=scores,
        winners=tuple(index for index, player_id in enumerate(player_ids) if player_id in result.winners),
        completed_hand_scores=hand_scores,
        partial_scores=partial_scores,
        ended_by=player_ids.index(result.ended_by) if result.ended_by is not None else None,
        reason=result.reason,
        actions_accepted=result.actions_accepted,
        actions_rejected=result.actions_rejected,
        seat_actions=result.seat_actions,
        duration_seconds=duration,
        seat_decision_seconds=tuple(sum(values) if len(values) > 0 else None for values in samples),
        seat_decision_calls=tuple(len(values) for values in samples),
        seat_decision_samples=samples,
        seat_stats=tuple(stats.get(player_id) for player_id in player_ids),
        stats_errors=errors,
        event_trace=event_trace,
        action_trace=action_trace,
    )


class _RunCollector:
    def __init__(
        self,
        plan: ArenaPlan,
        writer: ArenaRunWriter | None,
        abandoned: AbandonedDecisions,
        on_progress: Callable[[int], None] | None,
    ) -> None:
        self.plan = plan
        self.writer = writer
        self.abandoned = abandoned
        self.on_progress = on_progress
        self.started: list[str] = []
        self.results: dict[str, MatchRecord] = {}

    def status(self, fatal: BaseException | None = None, *, complete: bool = False) -> RunStatus:
        planned = tuple(job.job_id for job in self.plan.jobs)
        state = "running"
        if fatal is not None:
            state = "failed"
        elif complete:
            state = "completed" if len(self.results) == len(planned) else "stopped"
        return RunStatus(
            state=state,
            planned_job_ids=planned,
            started_job_ids=tuple(self.started),
            completed_job_ids=tuple(job_id for job_id in planned if job_id in self.results),
            lost_job_ids=tuple(job_id for job_id in self.started if job_id not in self.results),
            unstarted_job_ids=tuple(job_id for job_id in planned if job_id not in self.started),
            decisions_abandoned=self.abandoned.total,
            error=repr(fatal) if fatal is not None else None,
        )

    def on_started(self, index: int) -> None:
        self.started.append(self.plan.jobs[index].job_id)
        if self.writer is not None:
            self.writer.write_status(self.status())

    def accept(self, index: int, result: MatchRecord) -> bool:
        if result.job_id != self.plan.jobs[index].job_id:
            msg = "worker returned an outcome for a different job"
            raise ArenaError(msg)
        if self.writer is not None:
            self.writer.append(result)
        self.results[result.job_id] = result
        if self.writer is not None:
            self.writer.write_status(self.status())
        if self.on_progress is not None:
            callback = self.on_progress
            try:
                callback(len(self.results))
            except Exception:
                self.on_progress = None
                raise
        return result.outcome == MatchOutcome.FAILED


def _drive_plan(settings: _WorkerSettings, collector: _RunCollector, observer: Observer | None) -> BaseException | None:
    config = settings.config
    pool: Executor
    play_job: Callable[[PlannedMatch], MatchRecord]
    if config.backend == "process":
        pool = ProcessPoolExecutor(
            max_workers=config.concurrency,
            mp_context=get_context("spawn"),
            initializer=_initialise_worker,
            initargs=(settings, collector.abandoned.shared),
        )
        play_job = _play_process_job
    else:
        pool = ThreadPoolExecutor(max_workers=config.concurrency, thread_name_prefix="arena-match")
        play_job = partial(_play_job, settings, collector.abandoned, observer)
    with pool:
        _, fatal = _collect_work(
            pool, play_job, collector.plan.jobs, config, collector.abandoned, collector.accept, collector.on_started
        )
    return fatal


def run_plan(
    plan: ArenaPlan,
    *,
    config: RunConfig | None = None,
    output_dir: Path | str | None = None,
    trace: bool = False,
    observer: Observer | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> ArenaRun:
    """Collect outcomes with fresh anonymous seats, optionally saving a run.

    Without output_dir all run data stays in memory. Supplied output directories
    must not exist; optional traces use their traces subdirectory. An execution
    error carries its partial ArenaRun, whether or not it was saved.
    """
    if trace and output_dir is None:
        msg = "trace=True requires an output_dir"
        raise ValueError(msg)
    requested = plan.execution if config is None else config
    if requested.trace_dir is not None:
        msg = "planned arenas store all artifacts in output_dir; enable traces with trace=True"
        raise ValueError(msg)
    plan = with_execution(plan, requested)
    config = plan.execution
    specs = resolve_frozen_catalogue(plan.catalogue)
    if config.backend == "process":
        _validate_process_players(specs, observer)
    directory = Path(output_dir).resolve() if output_dir is not None else None
    initial = RunStatus(
        state="running",
        planned_job_ids=tuple(job.job_id for job in plan.jobs),
        unstarted_job_ids=tuple(job.job_id for job in plan.jobs),
    )
    writer = ArenaRunWriter(directory, plan, initial, trace=trace) if directory is not None else None
    provenance = writer.provenance if writer is not None else runtime_provenance(plan)
    shared = (
        SharedAbandonedState.create()
        if config.backend == "process" and config.decision_timeout_seconds is not None
        else None
    )
    abandoned = AbandonedDecisions(resolved_abandoned_limit(config), shared)
    collector = _RunCollector(plan, writer, abandoned, on_progress)
    settings = _WorkerSettings(
        {entry.config_id: spec for entry, spec in zip(plan.catalogue, specs, strict=True)},
        plan.rules,
        plan.protocol,
        replace(config, trace_dir=directory / "traces") if directory is not None and trace else config,
    )
    fatal = None
    try:
        fatal = _drive_plan(settings, collector, observer)
    except BaseException as error:
        fatal = error
    finally:
        status = collector.status(fatal, complete=True)
        if writer is not None:
            writer.write_status(status)
            writer.close()
    run = ArenaRun(
        plan=plan,
        results=tuple(collector.results[job.job_id] for job in plan.jobs if job.job_id in collector.results),
        status=status,
        artifact_dir=directory,
        provenance=provenance,
    )
    if fatal is not None:
        message = f"arena execution failed: {fatal}"
        raise PlannedArenaError(message, run) from fatal
    return run
