"""Construct fresh lineups and schedule bounded tournament workers."""

import hashlib
import pickle
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from dataclasses import dataclass
from functools import partial
from multiprocessing import get_context

from sixnimmt.arena.aggregation import _Aggregate, _GameSummary
from sixnimmt.arena.bots.base import Bot
from sixnimmt.arena.config import RunConfig, resolved_abandoned_limit
from sixnimmt.arena.decisions import AbandonedDecisions, SharedAbandonedState
from sixnimmt.arena.match import ArenaError, Observer, _Match, _run_match
from sixnimmt.arena.players import ResolvedPlayer
from sixnimmt.arena.results import MatchOutcome
from sixnimmt.arena.tracing import collect_stats
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.state import PlayerSeat
from sixnimmt.persistence.manifest import ManifestMatch
from sixnimmt.persistence.sink import JsonlEventSink, NullEventSink

_process_job: "_GameJob | None" = None
_process_abandoned: AbandonedDecisions | None = None


@dataclass(frozen=True)
class _GameJob:
    seed: int
    specs: Sequence[ResolvedPlayer]
    seats: Sequence[PlayerSeat]
    rules: GameRules
    protocol: MatchProtocol
    config: RunConfig
    run_id: str


def derive_seed(seed: int, domain: str, game_index: int, seat_index: int | None = None) -> int:
    """Stable independent streams, derived without consuming another RNG."""
    value = f"sixnimmt-arena:{seed}:{domain}:{game_index}:{seat_index}"
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")


def _play_game(
    index: int,
    seed: int,
    specs: Sequence[ResolvedPlayer],
    seats: Sequence[PlayerSeat],
    rules: GameRules,
    protocol: MatchProtocol,
    config: RunConfig,
    run_id: str,
    abandoned: AbandonedDecisions,
    observer: Observer | None,
    initial_bots: Sequence[Bot] | None = None,
) -> tuple[_GameSummary, ManifestMatch | None]:
    match_seed = derive_seed(seed, "match", index)
    match_id = f"arena_{index}"
    filename = f"{run_id}_{index}"
    sink = JsonlEventSink(config.trace_dir, filename) if config.trace_dir is not None else NullEventSink()
    bots: list[Bot] = list(initial_bots) if initial_bots is not None else []
    try:
        build_error = None
        if initial_bots is None:
            for seat, spec in enumerate(specs):
                try:
                    bots.append(spec.build(derive_seed(seed, "bot", index, seat)))
                except Exception as error:
                    build_error = error
                    break
        if build_error is not None:
            if index == 0 and config.backend == "process":
                msg = f"cannot construct first game's lineup: {build_error!r}"
                raise ArenaError(msg) from build_error
            match = _Match(match_seed, match_id, seats, rules, protocol, config, sink, observer)
            result = match.finish(MatchOutcome.FAILED, f"player_{len(bots) + 1}", repr(build_error))
        else:
            result = _run_match(
                bots,
                match_seed,
                match_id=match_id,
                rules=rules,
                protocol=protocol,
                config=config,
                seats=seats,
                observer=observer,
                sink=sink,
                _abandoned=abandoned,
            )
        stats, errors = collect_stats(bots, result.ended_by if result.reason == "decision_timeout" else None)
        for seat in seats:
            stats.setdefault(seat.player_id, None)
        entry = ManifestMatch(
            game_index=index,
            match_id=match_id,
            seed=match_seed,
            outcome=result.outcome.value,
            winners=result.winners,
            ended_by=result.ended_by,
            reason=result.reason,
            log=f"{filename}.jsonl",
            actions=f"{filename}.actions.jsonl",
            seat_stats=stats,
            stats_errors=errors,
        )
        return _GameSummary.from_result(result), entry if config.trace_dir is not None else None
    finally:
        sink.close()


def _initialise_process_worker(job: _GameJob, shared: SharedAbandonedState | None) -> None:
    global _process_job, _process_abandoned
    _process_job = job
    _process_abandoned = AbandonedDecisions(resolved_abandoned_limit(job.config), shared)


def _play_process_game(index: int) -> tuple[_GameSummary, ManifestMatch | None]:
    job = _process_job
    abandoned = _process_abandoned
    if job is None or abandoned is None:
        msg = "process worker was not initialized"
        raise ArenaError(msg)
    return _play_game(
        index, job.seed, job.specs, job.seats, job.rules, job.protocol, job.config, job.run_id, abandoned, None
    )


def _play_thread_game(
    job: _GameJob, abandoned: AbandonedDecisions, observer: Observer | None, initial_bots: Sequence[Bot], index: int
) -> tuple[_GameSummary, ManifestMatch | None]:
    return _play_game(
        index,
        job.seed,
        job.specs,
        job.seats,
        job.rules,
        job.protocol,
        job.config,
        job.run_id,
        abandoned,
        observer,
        initial_bots if index == 0 else None,
    )


def _collect_games(
    pool: Executor,
    play_game: Callable[[int], tuple[_GameSummary, ManifestMatch | None]],
    games: int,
    config: RunConfig,
    abandoned: AbandonedDecisions,
    aggregate: _Aggregate,
    entries: list[ManifestMatch],
    on_progress: Callable[[int], None] | None,
) -> tuple[int, BaseException | None]:
    collector = _LegacyCollector(aggregate, entries, on_progress)
    return _collect_work(
        pool, play_game, range(games), config, abandoned, collector.accept, propagate_callback_errors=True
    )


@dataclass
class _LegacyCollector:
    aggregate: _Aggregate
    entries: list[ManifestMatch]
    on_progress: Callable[[int], None] | None

    def accept(self, index: int, item: tuple[_GameSummary, ManifestMatch | None]) -> bool:
        result, entry = item
        self.aggregate.add(result, self.on_progress)
        if entry is not None:
            self.entries.append(entry)
        return result.outcome == MatchOutcome.FAILED


def _collect_work[T, W](
    pool: Executor,
    play_game: Callable[[W], T],
    jobs: Sequence[W],
    config: RunConfig,
    abandoned: AbandonedDecisions,
    accept: Callable[[int, T], bool],
    on_started: Callable[[int], None] | None = None,
    *,
    propagate_callback_errors: bool = False,
) -> tuple[int, BaseException | None]:
    """Bound submissions and drain every submitted future after a fatal error.

    The collector runs in the parent and returns whether an outcome failed.
    This keeps persistence and fixed-seat aggregation on the same scheduler.
    """
    games = len(jobs)
    started = 0
    fatal: BaseException | None = None
    stop = False
    pending: dict[Future[T], int] = {}
    while len(pending) > 0 or (started < games and not stop):
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
        completed, _ = wait(pending, return_when=FIRST_COMPLETED)
        for future in completed:
            index = pending.pop(future)
            failed, error = _receive_work(index, future, accept, propagate_callback_errors)
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


def _receive_work[T](
    index: int, future: Future[T], accept: Callable[[int, T], bool], propagate_callback_errors: bool
) -> tuple[bool, BaseException | None]:
    try:
        result = future.result()
    except Exception as error:
        return False, error
    if propagate_callback_errors:
        return accept(index, result), None
    try:
        return accept(index, result), None
    except Exception as error:
        return False, error


def _drive_games(
    games: int,
    job: _GameJob,
    abandoned: AbandonedDecisions,
    observer: Observer | None,
    initial_bots: Sequence[Bot],
    aggregate: _Aggregate,
    entries: list[ManifestMatch],
    on_progress: Callable[[int], None] | None,
) -> tuple[int, BaseException | None]:
    config = job.config
    pool: Executor
    play_game: Callable[[int], tuple[_GameSummary, ManifestMatch | None]]
    if config.backend == "process":
        pool = ProcessPoolExecutor(
            max_workers=config.concurrency,
            mp_context=get_context("spawn"),
            initializer=_initialise_process_worker,
            initargs=(job, abandoned.shared),
        )
        play_game = _play_process_game
    else:
        pool = ThreadPoolExecutor(max_workers=config.concurrency, thread_name_prefix="arena-match")
        play_game = partial(_play_thread_game, job, abandoned, observer, initial_bots)
    with pool:
        return _collect_games(pool, play_game, games, config, abandoned, aggregate, entries, on_progress)


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
