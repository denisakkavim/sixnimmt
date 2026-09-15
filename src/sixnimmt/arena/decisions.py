"""Measure trusted bot calls and bound waiting without blocking match workers."""

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from multiprocessing.sharedctypes import Synchronized
from multiprocessing.synchronize import Event as ProcessEvent
from multiprocessing.synchronize import Lock as ProcessLock
from queue import Empty, Queue
from time import monotonic

from sixnimmt.arena.bots.base import ActionBatch, Bot, Rejection, memory_bot
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
from sixnimmt.engine.views import MatchView


@dataclass(frozen=True)
class SharedAbandonedState:
    """Run-wide counters passed to spawned workers during initialization."""

    total: Synchronized
    active: Synchronized
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
class Decision:
    action: Action | ActionBatch | None
    error: BaseException | None
    timed_out: bool
    started_at: datetime
    ended_at: datetime
    duration_ms: float


def decide(
    bot: Bot,
    view: MatchView,
    rejection: Rejection | None,
    timeout: float | None,
    abandoned: AbandonedDecisions,
    stop_event: threading.Event | None = None,
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
    return Decision(action, error, timed_out, started_at, datetime.now(UTC), (monotonic() - started) * 1000)


def _wait_for_reply(
    replies: Queue[Action | ActionBatch | BaseException], timeout: float | None, stop_event: threading.Event | None
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
        if not 1 <= result.size <= 8:
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
