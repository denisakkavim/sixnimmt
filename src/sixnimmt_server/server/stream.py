"""Audience-filtered live streams and subscriber notifications."""

import asyncio
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from sixnimmt_server.engine.audience import Viewer, visible_to
from sixnimmt_server.engine.events import Event
from sixnimmt_server.engine.views import ViewRole

_ViewerKey = tuple[ViewRole, str | None]


def _key(viewer: Viewer) -> _ViewerKey:
    return (viewer.role, viewer.player_id)


class SinkClosed(Exception):
    """The match released its resources."""


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
