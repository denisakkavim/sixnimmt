"""Who may see an event, and each viewer's own gap-free position in their stream.

An event's audience is the only thing that decides who receives it. Views are
built by folding a viewer's filtered stream, so an event a viewer may not see
never reaches them and cannot leak through a field someone forgot to blank.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sixnimmt.engine.events import Event
from sixnimmt.engine.views import ViewRole

PUBLIC = "public"
ADMIN = "admin"
PLAYER_PREFIX = "player:"


@dataclass(frozen=True)
class Viewer:
    """A caller's identity for filtering purposes."""

    role: ViewRole
    player_id: str | None = None

    def __post_init__(self) -> None:
        if self.role == ViewRole.PLAYER and not self.player_id:
            msg = "a player viewer needs a player_id"
            raise ValueError(msg)


def addressed_player(event: Event) -> str | None:
    """The player an event is addressed to, or None when it is not player-scoped."""
    if not event.audience.startswith(PLAYER_PREFIX):
        return None
    return event.audience[len(PLAYER_PREFIX) :]


def visible_to(event: Event, viewer: Viewer) -> bool:
    """Whether this viewer receives this event at all."""
    if viewer.role == ViewRole.ADMIN:
        return True
    if event.audience == ADMIN:
        # Seeds and creation records stay admin-only, omniscient observers included.
        return False
    if event.audience == PUBLIC:
        return True
    recipient = addressed_player(event)
    if recipient is None:
        # An unrecognised audience is withheld rather than guessed at.
        return False
    if viewer.role == ViewRole.OMNISCIENT_OBSERVER:
        return True
    return viewer.role == ViewRole.PLAYER and recipient == viewer.player_id


def visible_events(events: Iterable[Event], viewer: Viewer) -> list[Event]:
    """The viewer's filtered subsequence, in match order."""
    return [event for event in events if visible_to(event, viewer)]


def view_version(events: Sequence[Event], viewer: Viewer) -> int:
    """The viewer's cursor: how many events they have seen, numbered from 1.

    Derived from the length of their own filtered stream, so it is gap-free by
    construction and cannot be advanced by an event they cannot see.
    """
    return len(visible_events(events, viewer))


def events_since(events: Sequence[Event], viewer: Viewer, since: int) -> list[tuple[int, Event]]:
    """Visible events after the caller's cursor, each paired with its cursor value.

    `since` is exclusive, so a caller at 0 receives their whole stream from 1.
    """
    if since < 0:
        msg = "since must not be negative"
        raise ValueError(msg)
    visible = visible_events(events, viewer)
    return [(index, event) for index, event in enumerate(visible, start=1) if index > since]
