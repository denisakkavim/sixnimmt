"""Execute every planned lineup with bounded workers and durable outcome collection."""

import hashlib
import pickle
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from contextlib import ExitStack
from dataclasses import dataclass, replace
from functools import partial
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic

from sixnimmt.arena.artifacts import ArenaRun, ArenaRunWriter, MatchRecord, RunStatus, runtime_provenance
from sixnimmt.arena.artifacts import load_run as load_run
from sixnimmt.arena.bots.base import Bot
from sixnimmt.arena.catalogue import validate_frozen_catalogue
from sixnimmt.arena.config import (
    RecordingPolicy,
    RunConfig,
    RunLimits,
    SessionOptions,
    resolve_settings,
    resolved_abandoned_limit,
)
from sixnimmt.arena.decisions import AbandonedDecisions, DecisionMetrics, SharedAbandonedState, StopSignal
from sixnimmt.arena.match import ActivityObserver, ArenaError, Observer, _BotLifecycle, _Match, _run_match
from sixnimmt.arena.planning import ArenaPlan, PlannedMatch, with_execution
from sixnimmt.arena.players import PlayerConfig, ResolvedPlayer, ResolvedStrategy, resolve_players, resolve_strategy
from sixnimmt.arena.results import MatchOutcome, MatchResult
from sixnimmt.arena.sessions import (
    EXTERNAL_SEATS,
    ConfirmationDispatcher,
    SeatConstructionError,
    SetupTimeout,
    prepare_seats,
    supervise_sessions,
)
from sixnimmt.arena.tracing import collect_stats
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.state import PlayerSeat
from sixnimmt.persistence.sink import EventSink, JsonlEventSink, NullEventSink


@dataclass(frozen=True)
class BotAssignment:
    strategy: ResolvedStrategy
    seed: int


@dataclass(frozen=True)
class MatchJob:
    """Concrete match identity; no population labels enter the observations."""

    match_id: str
    seed: int
    seats: tuple[PlayerSeat, ...]
    assignments: tuple[BotAssignment, ...] = ()
    trace_name: str | None = None


@dataclass(frozen=True)
class ConstructionFailure:
    seat: int
    cause: Exception


@dataclass(frozen=True)
class ExecutedMatch:
    result: MatchResult
    bots: tuple[Bot, ...]
    metrics: DecisionMetrics
    duration_seconds: float


def execute_match_job(
    job: MatchJob,
    rules: GameRules,
    protocol: MatchProtocol,
    config: RunConfig,
    abandoned: AbandonedDecisions,
    *,
    observer: Observer | None = None,
    on_activity: ActivityObserver | None = None,
    stop_event: StopSignal | None = None,
    initial_bots: Sequence[Bot] | None = None,
    construction_fatal: bool = False,
    construction_failure: ConstructionFailure | None = None,
    recording: RecordingPolicy | None = None,
    _lifecycle: _BotLifecycle | None = None,
) -> ExecutedMatch:
    """Own one trace and one match lifecycle for every execution mode."""
    if construction_failure is None and initial_bots is None and len(job.assignments) != len(job.seats):
        msg = "every seat requires a seeded bot assignment"
        raise ValueError(msg)
    if construction_failure is None and initial_bots is not None and len(initial_bots) != len(job.seats):
        msg = "seats and bots must have the same length"
        raise ValueError(msg)
    recording = resolve_settings(config, protocol).recording if recording is None else recording
    config = replace(config, trace_dir=recording.trace_dir)
    trace_name = job.match_id if job.trace_name is None else job.trace_name
    sink: EventSink = NullEventSink()
    lifecycle = _BotLifecycle() if _lifecycle is None else _lifecycle
    metrics = DecisionMetrics(tuple(seat.player_id for seat in job.seats), recording.retain_decision_samples)
    bots = list(initial_bots) if initial_bots is not None else []
    started = monotonic()
    try:
        if config.trace_dir is not None:
            sink = JsonlEventSink(config.trace_dir, trace_name)
        build_error = None if construction_failure is None else construction_failure.cause
        if initial_bots is None and construction_failure is None:
            for assignment in job.assignments:
                try:
                    bots.append(assignment.strategy.build(assignment.seed))
                except Exception as error:
                    build_error = error
                    break
        if build_error is not None:
            if construction_fatal:
                msg = f"cannot construct first game's lineup: {build_error!r}"
                raise ArenaError(msg) from build_error
            match = _Match(job.seed, job.match_id, job.seats, rules, protocol, config, sink, observer)
            failed_seat = len(bots) if construction_failure is None else construction_failure.seat
            result = match.finish(MatchOutcome.FAILED, job.seats[failed_seat].player_id, repr(build_error))
        else:
            result = _run_match(
                bots,
                job.seed,
                match_id=job.match_id,
                rules=rules,
                protocol=protocol,
                config=config,
                seats=job.seats,
                observer=observer,
                on_activity=on_activity,
                stop_event=stop_event,
                sink=sink,
                _abandoned=abandoned,
                _metrics=metrics,
                _lifecycle=lifecycle,
            )
        lifecycle.close_unstarted(bots, job.seats)
        result = replace(result, lifecycle_errors=tuple(lifecycle.errors))
        return ExecutedMatch(result, tuple(bots), metrics, monotonic() - started)
    finally:
        lifecycle.close_unstarted(bots, job.seats)
        sink.close()


def derive_seed(seed: int, domain: str, game_index: int, seat_index: int | None = None) -> int:
    """Stable independent streams, derived without consuming another RNG."""
    value = f"sixnimmt-arena:{seed}:{domain}:{game_index}:{seat_index}"
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")


def worker_pool(config: RunLimits, initializer: Callable[[], None]) -> Executor:
    if config.backend == "process":
        return ProcessPoolExecutor(
            max_workers=config.concurrency,
            mp_context=get_context("spawn"),
            initializer=initializer,
        )
    return ThreadPoolExecutor(max_workers=config.concurrency, thread_name_prefix="arena-match")


def _collect_work[T, W](
    pool: Executor,
    play_game: Callable[[W], T],
    jobs: Sequence[W],
    config: RunConfig,
    abandoned: AbandonedDecisions,
    accept: Callable[[int, T], bool],
    on_started: Callable[[int], None] | None = None,
    *,
    stop_event: StopSignal | None = None,
    close_event: StopSignal | None = None,
    worker_stop: StopSignal | None = None,
    on_poll: Callable[[], None] | None = None,
) -> tuple[int, BaseException | None]:
    """Bound submissions and drain every submitted future after a fatal error.

    The collector runs in the parent and returns whether an outcome failed.
    Persistence receives each completed outcome after its worker returns.
    """
    games = len(jobs)
    started = 0
    fatal: BaseException | None = None
    stop = False
    pending: dict[Future[T], int] = {}
    while len(pending) > 0 or (started < games and not stop):
        stop = _propagate_stop(stop_event, worker_stop) or stop
        while started < games and len(pending) < config.concurrency and not stop and not abandoned.exceeded.is_set():
            try:
                future = pool.submit(play_game, jobs[started])
                pending[future] = started
                started += 1
                if on_started is not None:
                    on_started(started - 1)
            except Exception as error:
                fatal = error if fatal is None else fatal
                stop = True
                break
        if len(pending) == 0:
            break
        completed = _wait_for_work(pending, stop_event, close_event, on_poll)
        for future in completed:
            index = pending.pop(future)
            failed, error = _receive_work(index, future, accept)
            if error is not None:
                fatal = error if fatal is None else fatal
                stop = True
                continue
            if config.stop_on_failure and failed:
                stop = True
        if abandoned.exceeded.is_set():
            stop = True
            fatal = (
                ArenaError("max_abandoned_decisions exceeded; stopped submitting matches") if fatal is None else fatal
            )
    return started, fatal


def _propagate_stop(stop_event: StopSignal | None, worker_stop: StopSignal | None) -> bool:
    if stop_event is None or not stop_event.is_set():
        return False
    if worker_stop is not None:
        worker_stop.set()
    return True


def _wait_for_work[T](
    pending: dict[Future[T], int],
    stop_event: StopSignal | None,
    close_event: StopSignal | None,
    on_poll: Callable[[], None] | None,
) -> set[Future[T]]:
    try:
        if on_poll is not None:
            on_poll()
        completed, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
    except KeyboardInterrupt:
        if stop_event is None:
            raise
        if stop_event.is_set() and close_event is not None:
            close_event.set()
        stop_event.set()
        return set()
    return completed


def _receive_work[T](
    index: int, future: Future[T], accept: Callable[[int, T], bool]
) -> tuple[bool, BaseException | None]:
    try:
        result = future.result()
        failed = accept(index, result)
    except Exception as error:
        return False, error
    return failed, None


def _validate_process_players(specs: Sequence[ResolvedPlayer], observer: Observer | None) -> None:
    if observer is not None:
        msg = "process backend does not support observer callbacks; use traces or the thread backend"
        raise ValueError(msg)
    for spec in specs:
        try:
            pickle.dumps(spec)
        except Exception as error:
            msg = (
                f"player {spec.name!r} cannot be sent to process workers; "
                "use an importable module-level factory and options model, not a lambda or local definition"
            )
            raise ValueError(msg) from error


class RunExecutionError(ArenaError):
    """Execution failed; the attached run retains every collected outcome."""

    def __init__(self, message: str, run: ArenaRun) -> None:
        detail = message if run.artifact_dir is None else f"{message}; saved run: {run.artifact_dir}"
        super().__init__(detail)
        self.run = run
        self.artifact_dir = run.artifact_dir


@dataclass(frozen=True)
class _WorkerSettings:
    players: dict[str, PlayerConfig]
    strategies: dict[str, ResolvedStrategy]
    rules: GameRules
    protocol: MatchProtocol
    config: RunConfig
    session: SessionOptions
    workspace: Path
    watch: bool = False
    cancellable: bool = False
    worker_stop: StopSignal | None = None


@dataclass(frozen=True)
class _RunHooks:
    stopped: StopSignal
    closed: StopSignal
    observer: Observer | None = None
    on_activity: ActivityObserver | None = None
    report: Callable[[str], None] | None = None
    confirm_start: Callable[[], bool] | None = None
    on_poll: Callable[[], None] | None = None


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
    stopped = _worker_settings.worker_stop
    if stopped is None:
        msg = "process worker has no shared cancellation signal"
        raise ArenaError(msg)
    return _play_job(_worker_settings, _worker_abandoned, _RunHooks(stopped, Event()), job)


def _play_job(
    settings: _WorkerSettings, abandoned: AbandonedDecisions, hooks: _RunHooks, job: PlannedMatch
) -> MatchRecord:
    players = [settings.players[seat.config_id] for seat in job.seats]
    directory = settings.workspace / "matches" / job.match_id
    recording = RecordingPolicy(settings.config.trace_dir, settings.config.trace_dir is not None)
    try:
        prepared = prepare_seats(
            players,
            seed=job.match_seed,
            directory=directory,
            rules=settings.rules,
            protocol=settings.protocol,
            session_options=settings.session,
            bot_seeds=[seat.bot_seed for seat in job.seats],
            strategies=[settings.strategies.get(seat.config_id) for seat in job.seats],
        )
    except SeatConstructionError as error:
        seats = tuple(
            PlayerSeat(
                player_id=f"player_{index + 1}",
                display_name=(
                    (player.bot if player.display_name is None else player.display_name)
                    if job.stream == "fixed"
                    else f"Player {index + 1}"
                ),
            )
            for index, player in enumerate(players)
        )
        executed = execute_match_job(
            MatchJob(job.match_id, job.match_seed, seats),
            settings.rules,
            settings.protocol,
            settings.config,
            abandoned,
            observer=hooks.observer,
            initial_bots=(),
            construction_failure=ConstructionFailure(error.seat, error.cause),
            recording=recording,
        )
        return _record_result(
            job, executed.result, executed.bots, executed.metrics, executed.duration_seconds, settings
        )
    # Only explicit fixed lineups retain strategy labels in observations.
    seats = tuple(
        seat.seat
        if job.stream == "fixed"
        else PlayerSeat(player_id=seat.seat.player_id, display_name=f"Player {index + 1}")
        for index, seat in enumerate(prepared)
    )
    lifecycle = _BotLifecycle()
    with supervise_sessions(
        prepared,
        directory,
        settings.session,
        lifecycle,
        hooks.stopped,
        report=hooks.report,
        confirm_start=hooks.confirm_start,
        watch=settings.watch,
        closed=hooks.closed,
    ) as supervisor:
        executed = execute_match_job(
            MatchJob(job.match_id, job.match_seed, seats),
            settings.rules,
            settings.protocol,
            settings.config,
            abandoned,
            observer=hooks.observer,
            on_activity=hooks.on_activity,
            stop_event=hooks.stopped if settings.cancellable else None,
            initial_bots=[seat.bot for seat in prepared],
            recording=recording,
            _lifecycle=lifecycle,
        )
        record = _record_result(
            job, executed.result, executed.bots, executed.metrics, executed.duration_seconds, settings
        )
        supervisor.retain()
        return record


def _record_result(
    job: PlannedMatch,
    result: MatchResult,
    bots: Sequence[Bot],
    sink: DecisionMetrics,
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
    stats, errors = collect_stats(
        bots,
        result.ended_by if result.reason in ("decision_timeout", "operator_stop") else None,
        lifecycle_errors=result.lifecycle_errors,
    )
    samples = tuple(tuple(sink.samples[player_id]) for player_id in player_ids) if sink.retain_samples else ()
    if sink.retain_samples:
        for player_id in player_ids:
            sink.seconds[player_id] = sum(sink.samples[player_id])
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
        seat_decision_seconds=tuple(
            sink.seconds[player_id] if sink.calls[player_id] > 0 else None for player_id in player_ids
        ),
        seat_decision_calls=tuple(sink.calls[player_id] for player_id in player_ids),
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

    def status(self, fatal: BaseException | None = None, *, complete: bool = False, stopped: bool = False) -> RunStatus:
        planned = tuple(job.job_id for job in self.plan.jobs)
        state = "running"
        operator_stopped = stopped and (fatal is None or isinstance(fatal, SetupTimeout))
        if operator_stopped:
            state = "stopped"
        elif fatal is not None:
            state = "failed"
        elif complete:
            state = "completed" if len(self.results) == len(planned) and not stopped else "stopped"
        return RunStatus(
            state=state,
            planned_job_ids=planned,
            started_job_ids=tuple(self.started),
            completed_job_ids=tuple(job_id for job_id in planned if job_id in self.results),
            lost_job_ids=tuple(job_id for job_id in self.started if job_id not in self.results),
            unstarted_job_ids=tuple(job_id for job_id in planned if job_id not in self.started),
            decisions_abandoned=self.abandoned.total,
            error="operator_stop" if operator_stopped else (repr(fatal) if fatal is not None else None),
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


def _drive_plan(settings: _WorkerSettings, collector: _RunCollector, hooks: _RunHooks) -> BaseException | None:
    config = settings.config
    pool = worker_pool(
        resolve_settings(config, settings.protocol).run,
        partial(_initialise_worker, settings, collector.abandoned.shared),
    )
    play_job: Callable[[PlannedMatch], MatchRecord]
    if config.backend == "process":
        play_job = _play_process_job
    else:
        play_job = partial(_play_job, settings, collector.abandoned, hooks)
    with pool:
        _, fatal = _collect_work(
            pool,
            play_job,
            collector.plan.jobs,
            config,
            collector.abandoned,
            collector.accept,
            collector.on_started,
            stop_event=hooks.stopped,
            close_event=hooks.closed,
            worker_stop=settings.worker_stop,
            on_poll=hooks.on_poll,
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
    players: Mapping[str, PlayerConfig] | None = None,
    session: SessionOptions | None = None,
    watch: bool = False,
    on_activity: ActivityObserver | None = None,
    report: Callable[[str], None] | None = None,
    confirm_start: Callable[[], bool] | None = None,
    stop_event: StopSignal | None = None,
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
    validate_frozen_catalogue(plan.catalogue, players)
    construction = {
        entry.config_id: PlayerConfig(
            bot=entry.bot,
            display_name=entry.label,
            agent_metadata={}
            if players is None or entry.config_id not in players
            else players[entry.config_id].agent_metadata,
            options=entry.options
            if players is None or entry.config_id not in players
            else players[entry.config_id].options,
        )
        for entry in plan.catalogue
    }
    strategies = {
        config_id: resolve_strategy(player.bot, player.options)
        for config_id, player in construction.items()
        if player.bot not in EXTERNAL_SEATS
    }
    external = [player for player in construction.values() if player.bot in EXTERNAL_SEATS]
    attached = any(EXTERNAL_SEATS[player.bot].ownership == "attached" for player in external)
    if (watch or attached) and (config.backend != "thread" or config.concurrency != 1):
        msg = "watched runs and attached sessions require the thread backend with concurrency=1"
        raise ValueError(msg)
    if config.backend == "process":
        if len(external) > 0 or on_activity is not None:
            msg = "external sessions and activity callbacks require the thread backend"
            raise ValueError(msg)
        _validate_process_players(resolve_players(list(construction.values())), observer)
    directory = Path(output_dir).resolve() if output_dir is not None else None
    initial = RunStatus(
        state="running",
        planned_job_ids=tuple(job.job_id for job in plan.jobs),
        unstarted_job_ids=tuple(job.job_id for job in plan.jobs),
    )
    cancellable = watch or len(external) > 0 or stop_event is not None
    with ExitStack() as resources:
        writer = (
            ArenaRunWriter(directory, plan, initial, trace=trace, session=session) if directory is not None else None
        )
        if writer is not None:
            resources.callback(writer.close)
        provenance = writer.provenance if writer is not None else runtime_provenance(plan, session=session)
        shared = (
            SharedAbandonedState.create()
            if config.backend == "process" and (config.decision_timeout_seconds is not None or cancellable)
            else None
        )
        worker_stop = get_context("spawn").Event() if config.backend == "process" else None
        abandoned = AbandonedDecisions(resolved_abandoned_limit(config), shared)
        collector = _RunCollector(plan, writer, abandoned, on_progress)
        stopped = Event() if stop_event is None else stop_event
        confirmation = (
            ConfirmationDispatcher(confirm_start, stopped)
            if confirm_start is not None and config.backend == "thread"
            else None
        )
        hooks = _RunHooks(
            stopped,
            Event(),
            observer,
            on_activity,
            report,
            confirmation.request if confirmation is not None else None,
            confirmation.poll if confirmation is not None else None,
        )
        fatal = None
        with TemporaryDirectory(prefix="sixnimmt-run-") as temporary:
            settings = _WorkerSettings(
                construction,
                strategies,
                plan.rules,
                plan.protocol,
                replace(config, trace_dir=directory / "traces") if directory is not None and trace else config,
                SessionOptions() if session is None else session,
                Path(temporary) if directory is None else directory,
                watch,
                cancellable,
                worker_stop,
            )
            try:
                fatal = _drive_plan(settings, collector, hooks)
            except BaseException as error:
                fatal = error
            finally:
                status = collector.status(fatal, complete=True, stopped=hooks.stopped.is_set())
                if writer is not None:
                    writer.write_status(status)
    run = ArenaRun(
        plan=plan,
        results=tuple(collector.results[job.job_id] for job in plan.jobs if job.job_id in collector.results),
        status=status,
        artifact_dir=directory,
        provenance=provenance,
    )
    if fatal is not None:
        message = f"arena execution failed: {fatal}"
        raise RunExecutionError(message, run) from fatal
    return run
