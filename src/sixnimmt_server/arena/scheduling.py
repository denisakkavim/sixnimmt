"""Policies for offering selecting seats a decision, never for action legality."""

from typing import Protocol

from sixnimmt_server.engine.state import MatchState


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
