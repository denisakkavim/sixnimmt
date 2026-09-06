"""Measure trusted bot calls and bound waiting without blocking match workers."""

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from queue import Empty, Queue
from time import monotonic

from sixnimmt_server.arena.bots import Bot, Rejection
from sixnimmt_server.arena.bots.base import ActionBatch
from sixnimmt_server.engine.actions import (
    Action,
    ChooseRowAction,
    CommitAction,
    SelectCardAction,
    SendMessageAction,
    UncommitAction,
)
from sixnimmt_server.engine.views import MatchView


class AbandonedDecisions:
    """Count timed-out calls still running; completed late results are discarded."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self.exceeded = threading.Event()

    def add(self, thread: threading.Thread) -> None:
        with self._lock:
            self.total += 1
            self._threads = [item for item in self._threads if item.is_alive()]
            if thread.is_alive():
                self._threads.append(thread)
            if len(self._threads) > self.limit:
                self.exceeded.set()


@dataclass(frozen=True)
class Decision:
    action: Action | ActionBatch | None
    error: BaseException | None
    timed_out: bool
    started_at: datetime
    ended_at: datetime
    duration_ms: float


def decide(
    bot: Bot, view: MatchView, rejection: Rejection | None, timeout: float | None, abandoned: AbandonedDecisions
) -> Decision:
    started_at = datetime.now(UTC)
    started = monotonic()
    action = None
    error = None
    timed_out = False

    if timeout is None:
        try:
            action = _call_bot(bot, view, rejection)
        except Exception as caught:
            error = caught
    else:
        replies: Queue[Action | ActionBatch | BaseException] = Queue(maxsize=1)

        # Python cannot interrupt a blocking call. This backstop frees the match
        # worker; providers must still impose their own client-side timeouts.
        thread = threading.Thread(
            target=_queue_decision, args=(bot, view, rejection, replies), daemon=True, name="arena-decision"
        )
        thread.start()
        try:
            reply = replies.get(timeout=timeout)
        except Empty:
            timed_out = True
            abandoned.add(thread)
        else:
            if isinstance(reply, BaseException):
                error = reply
            else:
                action = reply
    return Decision(action, error, timed_out, started_at, datetime.now(UTC), (monotonic() - started) * 1000)


def _call_bot(bot: Bot, view: MatchView, rejection: Rejection | None) -> Action | ActionBatch:
    result = bot.act(view, rejection)
    if isinstance(result, ActionBatch):
        if not 1 <= result.size <= 8:
            msg = "a batch must contain one to eight calls"
            raise ValueError(msg)
        if result.memory is not None and not callable(getattr(bot, "accept_batch", None)):
            msg = "bot does not support transactional memory"
            raise TypeError(msg)
        return result
    if not isinstance(result, (SelectCardAction, CommitAction, UncommitAction, SendMessageAction, ChooseRowAction)):
        msg = f"bot returned {type(result).__name__}, expected an Action"
        raise TypeError(msg)
    return result


def _queue_decision(
    bot: Bot, view: MatchView, rejection: Rejection | None, replies: Queue[Action | ActionBatch | BaseException]
) -> None:
    try:
        replies.put(_call_bot(bot, view, rejection))
    except BaseException as error:
        replies.put(error)
