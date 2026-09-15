"""Measure trusted bot calls and bound waiting without blocking match workers."""

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from multiprocessing.sharedctypes import Synchronized
from multiprocessing.synchronize import Event as ProcessEvent
from multiprocessing.synchronize import Lock as ProcessLock
from queue import Empty, Queue
from time import monotonic
from typing import Literal, Protocol

from sixnimmt.arena.bots.base import MAX_BATCH_OPERATIONS, ActionBatch, Bot, Rejection, memory_bot, observe_bot
from sixnimmt.arena.bots.lifecycle import (
    ControllerStopped,
    DecisionContext,
    DecisionDeadlineExceeded,
    set_decision_context,
)
from sixnimmt.engine.actions import (
    Action,
    ChooseRowAction,
    CommitAction,
    SelectCardAction,
    SendMessageAction,
    UncommitAction,
)
from sixnimmt.engine.errors import EngineRejection, ErrorCode
from sixnimmt.engine.events import Event
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.state import MatchState
from sixnimmt.engine.transition import transition
from sixnimmt.engine.views import MatchView


class StopSignal(Protocol):
    """Cancellation shared by local threads or spawned match workers."""

    def is_set(self) -> bool: ...

    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


@dataclass(frozen=True)
class SharedAbandonedState:
    """Run-wide counters passed to spawned workers during initialization."""

    total: "Synchronized[int]"
    active: "Synchronized[int]"
    lock: ProcessLock
    exceeded: ProcessEvent

    @classmethod
    def create(cls) -> "SharedAbandonedState":
        context = get_context("spawn")
        return cls(context.Value("q", 0), context.Value("q", 0), context.Lock(), context.Event())


class AbandonedDecisions:
    """Count timed-out calls still running; completed late results are discarded."""

    def __init__(self, limit: int, shared: SharedAbandonedState | None = None) -> None:
        self.limit = limit
        self.shared = shared
        self._total = 0
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self.exceeded = shared.exceeded if shared is not None else threading.Event()

    @property
    def total(self) -> int:
        if self.shared is not None:
            return self.shared.total.value
        return self._total

    def add(self, thread: threading.Thread) -> None:
        if self.shared is not None:
            self._add_shared(thread, self.shared)
            return
        with self._lock:
            self._total += 1
            self._threads = [item for item in self._threads if item.is_alive()]
            if thread.is_alive():
                self._threads.append(thread)
            if len(self._threads) > self.limit:
                self.exceeded.set()

    def _add_shared(self, thread: threading.Thread, shared: SharedAbandonedState) -> None:
        with shared.lock:
            shared.total.value += 1
            if not thread.is_alive():
                return
            shared.active.value += 1
            if shared.active.value > self.limit:
                shared.exceeded.set()
        # A worker can run more matches while an earlier decision is still alive.
        # Release its run-wide slot when that call ends, even between matches.
        watcher = threading.Thread(target=_release_abandoned_slot, args=(thread, shared), daemon=True)
        watcher.start()


def _release_abandoned_slot(thread: threading.Thread, shared: SharedAbandonedState) -> None:
    thread.join()
    with shared.lock:
        shared.active.value -= 1


@dataclass(frozen=True)
class DecisionTiming:
    started_at: datetime
    ended_at: datetime
    duration_ms: float


@dataclass(frozen=True)
class ActionDecision(DecisionTiming):
    action: Action | ActionBatch
    error: None = field(default=None, init=False)
    timed_out: Literal[False] = field(default=False, init=False)


@dataclass(frozen=True)
class FailedDecision(DecisionTiming):
    error: BaseException
    action: None = field(default=None, init=False)
    timed_out: Literal[False] = field(default=False, init=False)


@dataclass(frozen=True)
class TimedOutDecision(DecisionTiming):
    action: None = field(default=None, init=False)
    error: None = field(default=None, init=False)
    timed_out: Literal[True] = field(default=True, init=False)


type Decision = ActionDecision | FailedDecision | TimedOutDecision


def decide(
    bot: Bot,
    view: MatchView,
    rejection: Rejection | None,
    timeout: float | None,
    abandoned: AbandonedDecisions,
    stop_event: StopSignal | None = None,
) -> Decision:
    started_at = datetime.now(UTC)
    started = monotonic()
    action = None
    error = None
    timed_out = False
    context = DecisionContext(None if timeout is None else started_at + timedelta(seconds=timeout))

    if stop_event is not None and stop_event.is_set():
        error = ControllerStopped()
    elif timeout is None and stop_event is None:
        try:
            action = _call_bot(bot, view, rejection, context)
        except Exception as caught:
            error = caught
    else:
        replies: Queue[Action | ActionBatch | BaseException] = Queue(maxsize=1)

        # Python cannot interrupt a blocking call. This backstop frees the match
        # worker; providers must still impose their own client-side timeouts.
        thread = threading.Thread(
            target=_queue_decision, args=(bot, view, rejection, context, replies), daemon=True, name="arena-decision"
        )
        thread.start()
        try:
            reply = _wait_for_reply(replies, timeout, stop_event)
        except Empty:
            timed_out = True
            abandoned.add(thread)
        except ControllerStopped as stopped:
            error = stopped
            abandoned.add(thread)
        else:
            if isinstance(reply, BaseException):
                error = reply
            else:
                action = reply
    if isinstance(error, DecisionDeadlineExceeded):
        timed_out = True
        error = None
    timing = DecisionTiming(started_at, datetime.now(UTC), (monotonic() - started) * 1000)
    return _decision_result(action, error, timed_out, timing)


def _decision_result(
    action: Action | ActionBatch | None, error: BaseException | None, timed_out: bool, timing: DecisionTiming
) -> Decision:
    if timed_out:
        return TimedOutDecision(timing.started_at, timing.ended_at, timing.duration_ms)
    if error is not None:
        return FailedDecision(timing.started_at, timing.ended_at, timing.duration_ms, error)
    if action is None:
        msg = "decision completed without an action or failure"
        raise RuntimeError(msg)
    return ActionDecision(timing.started_at, timing.ended_at, timing.duration_ms, action)


def _wait_for_reply(
    replies: Queue[Action | ActionBatch | BaseException], timeout: float | None, stop_event: StopSignal | None
) -> Action | ActionBatch | BaseException:
    deadline = None if timeout is None else monotonic() + timeout
    while True:
        if stop_event is not None and stop_event.is_set():
            raise ControllerStopped
        remaining = None if deadline is None else deadline - monotonic()
        if remaining is not None and remaining <= 0:
            raise Empty
        interval = remaining
        if stop_event is not None:
            interval = 0.05 if remaining is None else min(remaining, 0.05)
        try:
            return replies.get(timeout=interval)
        except Empty:
            if stop_event is None:
                raise


def _call_bot(bot: Bot, view: MatchView, rejection: Rejection | None, context: DecisionContext) -> Action | ActionBatch:
    set_decision_context(bot, context)
    observe_bot(bot, view)
    result = bot.act(view, rejection)
    if isinstance(result, ActionBatch):
        if not isinstance(result.actions, tuple):
            msg = "batch actions must be a tuple"
            raise TypeError(msg)
        for action in result.actions:
            _validate_action(action)
        if result.memory is not None and not isinstance(result.memory, str):
            msg = "batch memory must be a string or None"
            raise TypeError(msg)
        if not 1 <= result.size <= MAX_BATCH_OPERATIONS:
            msg = "a batch must contain one to eight calls"
            raise ValueError(msg)
        if result.memory is not None and memory_bot(bot) is None:
            msg = "bot does not support transactional memory"
            raise TypeError(msg)
        return result
    _validate_action(result)
    return result


def _validate_action(result: object) -> None:
    if not isinstance(result, (SelectCardAction, CommitAction, UncommitAction, SendMessageAction, ChooseRowAction)):
        msg = f"bot returned {type(result).__name__}, expected an Action"
        raise TypeError(msg)


def _queue_decision(
    bot: Bot,
    view: MatchView,
    rejection: Rejection | None,
    context: DecisionContext,
    replies: Queue[Action | ActionBatch | BaseException],
) -> None:
    try:
        replies.put(_call_bot(bot, view, rejection, context))
    except BaseException as error:
        replies.put(error)


@dataclass
class DecisionMetrics:
    player_ids: Sequence[str]
    retain_samples: bool = False
    seconds: dict[str, float] = field(init=False)
    calls: dict[str, int] = field(init=False)
    samples: dict[str, list[float]] = field(init=False)

    def __post_init__(self) -> None:
        self.seconds = dict.fromkeys(self.player_ids, 0.0)
        self.calls = dict.fromkeys(self.player_ids, 0)
        self.samples = {player_id: [] for player_id in self.player_ids}

    def record_call(self, player_id: str, duration_ms: float) -> None:
        """Count calls, including batches rejected before their first action."""
        seconds = duration_ms / 1000
        self.seconds[player_id] += seconds
        self.calls[player_id] += 1
        if self.retain_samples:
            self.samples[player_id].append(seconds)


class Scheduler(Protocol):
    def next_seat(self, state: MatchState) -> int | None: ...


class SequentialScheduler(Scheduler):
    """Offer the first uncommitted seat; suitable for classic matches only."""

    def next_seat(self, state: MatchState) -> int | None:
        return next((index for index, player in enumerate(state.players) if not player.committed), None)


class RoundRobinScheduler(Scheduler):
    """Resume after the last offered seat, skipping committed players."""

    def __init__(self) -> None:
        self._last_seat = -1

    def next_seat(self, state: MatchState) -> int | None:
        for offset in range(1, len(state.players) + 1):
            index = (self._last_seat + offset) % len(state.players)
            if not state.players[index].committed:
                self._last_seat = index
                return index
        return None


@dataclass(frozen=True)
class PreparedBatch:
    steps: tuple[tuple[MatchState, list[Event]], ...]
    rejection: Rejection | None = None


def prepare_batch(
    state: MatchState, player_id: str, batch: ActionBatch, protocol: MatchProtocol, rules: GameRules
) -> PreparedBatch:
    steps = []
    terminal = False
    for index, action in enumerate(batch.actions):
        if terminal:
            message = (
                f"Game action {index + 1}: cannot follow a terminal action. Nothing was applied; memory is unchanged."
            )
            return PreparedBatch((), Rejection(ErrorCode.WRONG_PHASE, message, (), action))
        try:
            before = (state.hand_number, state.play_number, state.phase)
            state, events = transition(state, player_id, action, protocol, rules)
            after = (state.hand_number, state.play_number, state.phase)
            committed = next(player.committed for player in state.players if player.player_id == player_id)
            terminal = action.type in ("commit", "choose_row") or before != after or committed
            steps.append((state, events))
        except EngineRejection as error:
            message = f"Game action {index + 1} ({action.type.value}) failed: {error}. Nothing was applied; memory is unchanged."
            return PreparedBatch((), Rejection(error.code, message, (), action))
    return PreparedBatch(tuple(steps))
