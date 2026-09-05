"""Where a match's events are kept, how they reach subscribers, and how they persist.

Two things live here, and they answer different questions. `LiveEventStream`
holds the match in memory and hands each subscriber their own filtered slice of
it. `EventSink` is where events go to survive the process; `JsonlEventSink` is
the file-backed implementation, and the engine never learns which one is in use.

Fan-out happens after audience filtering, never before. A subscriber is handed
an event only if it is already in their visible stream, so an event they may not
see cannot wake them — the timing oracle §9.2 forbids is closed by the shape of
this module rather than by a check someone has to remember.
"""

import asyncio
import json
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Protocol

from pydantic import BaseModel, ConfigDict, TypeAdapter

from sixnimmt_server.engine.audience import Viewer, visible_to
from sixnimmt_server.engine.events import Event
from sixnimmt_server.engine.views import ViewRole

_ViewerKey = tuple[ViewRole, str | None]

_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


def _key(viewer: Viewer) -> _ViewerKey:
    return (viewer.role, viewer.player_id)


class SinkClosed(Exception):
    """Raised to a waiting subscriber when the match releases its resources."""


class ActionRecord(BaseModel):
    """One submitted action, in the order the server processed it (§11.1).

    `server_action_seq` is the canonical order; `received_at` is for latency
    analysis only, and nothing may replay a match from timestamps.
    """

    model_config = ConfigDict(frozen=True)

    server_action_seq: int
    action_id: str
    player_id: str
    type: str
    # An audit reference to the projection the client says it acted on. Recorded
    # so a decision can be read back against what the agent could see.
    from_view: str | None
    received_at: datetime


@dataclass
class Subscription:
    """One caller's live position in their own filtered stream."""

    viewer: Viewer
    cursor: int
    pending: deque[tuple[int, Event]] = field(default_factory=deque)
    signal: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False

    def deliver(self, cursor: int, event: Event) -> None:
        self.pending.append((cursor, event))
        self.signal.set()

    def close(self) -> None:
        self.closed = True
        self.signal.set()

    async def next_event(self, timeout: float) -> tuple[int, Event] | None:
        """The next visible event, or None on timeout.

        Everything already published is handed over before the closed flag is
        honoured, so abandoning a match cannot swallow the `match_abandoned`
        event that explains why the stream ended.
        """
        if self.pending:
            return self.pending.popleft()
        if self.closed:
            raise SinkClosed
        self.signal.clear()
        try:
            await asyncio.wait_for(self.signal.wait(), timeout)
        except TimeoutError:
            return None
        if self.pending:
            return self.pending.popleft()
        if self.closed:
            raise SinkClosed
        return None


class LiveEventStream:
    """Holds the match log in memory and serves each viewer their own slice."""

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._streams: dict[_ViewerKey, list[Event]] = {}
        self._subscriptions: list[Subscription] = []
        self._closed = False

    @property
    def events(self) -> list[Event]:
        """The whole log, for the store's own bookkeeping. Never served as-is."""
        return self._events

    def append(self, events: Sequence[Event]) -> None:
        for event in events:
            self._events.append(event)
            self._deliver(event)

    def _deliver(self, event: Event) -> None:
        for viewer_key, stream in self._streams.items():
            if visible_to(event, Viewer(role=viewer_key[0], player_id=viewer_key[1])):
                stream.append(event)
        for subscription in self._subscriptions:
            if not visible_to(event, subscription.viewer):
                continue
            subscription.cursor += 1
            subscription.deliver(subscription.cursor, event)

    def visible_stream(self, viewer: Viewer) -> list[Event]:
        """The viewer's filtered subsequence, cached and extended as events arrive."""
        viewer_key = _key(viewer)
        if viewer_key not in self._streams:
            self._streams[viewer_key] = [event for event in self._events if visible_to(event, viewer)]
        return self._streams[viewer_key]

    def view_version(self, viewer: Viewer) -> int:
        """The caller's gap-free cursor: how many events they have been sent."""
        return len(self.visible_stream(viewer))

    def events_since(self, viewer: Viewer, since: int, limit: int | None = None) -> list[tuple[int, Event]]:
        """Visible events after the caller's cursor, `since` exclusive."""
        stream = self.visible_stream(viewer)
        numbered = [(index, event) for index, event in enumerate(stream, start=1) if index > since]
        if limit is None:
            return numbered
        return numbered[:limit]

    def subscribe(self, viewer: Viewer) -> Subscription:
        """Start delivering this viewer's future events, numbered from where they are."""
        subscription = Subscription(viewer=viewer, cursor=self.view_version(viewer))
        if self._closed:
            subscription.close()
        self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    def close(self) -> None:
        """Release the match's subscribers; the log itself is retained (§11.4)."""
        self._closed = True
        for subscription in self._subscriptions:
            subscription.close()
        self._subscriptions = []


class EventSink(Protocol):
    """Where a match's events and action records go to outlive the process."""

    def append(self, events: Sequence[Event]) -> None: ...

    def record_action(self, record: ActionRecord) -> None: ...

    def close(self) -> None: ...


class NullEventSink:
    """Persists nothing. What an in-process match gets when no log is wanted."""

    def append(self, events: Sequence[Event]) -> None:
        return

    def record_action(self, record: ActionRecord) -> None:
        return

    def close(self) -> None:
        return


class JsonlEventSink:
    """One JSONL file per match, with its action records beside it.

    Nothing is filtered on the way in. The log is the durable record of the
    whole match, admin events and seeds included; audience filtering belongs to
    the moment a viewer reads, never to the moment the server writes.
    """

    def __init__(self, directory: Path, match_id: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        # Appending, never truncating: an existing log is history, and §11.4
        # requires it to survive everything that happens to the match.
        self._events = event_log_path(directory, match_id).open("a", encoding="utf-8")
        self._actions = action_log_path(directory, match_id).open("a", encoding="utf-8")
        self._closed = False

    def append(self, events: Sequence[Event]) -> None:
        for event in events:
            self._write(self._events, event.model_dump(mode="json"))

    def record_action(self, record: ActionRecord) -> None:
        self._write(self._actions, record.model_dump(mode="json"))

    def _write(self, handle: IO[str], payload: dict[str, Any]) -> None:
        if self._closed:
            # Abandonment releases the match's handles while its log stays on
            # disk. Nothing can be applied to the match afterwards, so a late
            # write has nothing left to record.
            return
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
        # Flushed as written (§11.3): a crash must not cost the tail of a match.
        handle.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._events.close()
        self._actions.close()


def event_log_path(directory: Path, match_id: str) -> Path:
    return directory / f"{match_id}.jsonl"


def action_log_path(directory: Path, match_id: str) -> Path:
    return directory / f"{match_id}.actions.jsonl"


def _lines(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield line


def read_event_log(path: Path) -> list[Event]:
    """Every event in a match log, in the order the server wrote it."""
    return [_EVENT_ADAPTER.validate_json(line) for line in _lines(path)]


def read_action_log(path: Path) -> list[ActionRecord]:
    """Every action record, already in `server_action_seq` order.

    Records are written inside the per-match serialisation boundary, so the file
    order is the processing order §11.1 makes canonical.
    """
    return [ActionRecord.model_validate_json(line) for line in _lines(path)]
