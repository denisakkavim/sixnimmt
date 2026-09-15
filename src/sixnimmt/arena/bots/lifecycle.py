"""Optional, short local hooks for bots owning a match session or external work."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sixnimmt.arena.bots.base import Bot, Rejection
from sixnimmt.arena.results import MatchOutcome
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.views import MatchView


class ControllerStopped(Exception):
    """An operator stopped the match while a bot was waiting for a decision."""


class DecisionDeadlineExceeded(TimeoutError):
    """A managed bot exhausted its decision budget before the outer deadline."""


@dataclass(frozen=True)
class BotContext:
    match_id: str
    player_id: str
    rules: GameRules
    protocol: MatchProtocol


@dataclass(frozen=True)
class DecisionContext:
    """The arena deadline in UTC; None means no arena deadline."""

    deadline: datetime | None


@dataclass(frozen=True)
class DecisionOutcome:
    """Publication outcome; failure does not promise rollback of storage writes."""

    status: Literal["accepted", "rejected", "failed"]
    rejection: Rejection | None = None
    reason: str | None = None


@dataclass(frozen=True)
class BotMatchEnd:
    """A seat's filtered terminal state, with public scores and a safe reason."""

    outcome: MatchOutcome
    winners: tuple[str, ...]
    scores: Mapping[str, int]
    reason: str | None
    final_view: MatchView


def set_decision_context(bot: Bot, context: DecisionContext) -> None:
    hook = getattr(bot, "set_decision_context", None)
    if callable(hook):
        hook(context)


def settle_decision(bot: Bot, outcome: DecisionOutcome) -> None:
    hook = getattr(bot, "settle_decision", None)
    if callable(hook):
        hook(outcome)


def cancel_bot(bot: Bot, reason: str) -> None:
    hook = getattr(bot, "cancel", None)
    if callable(hook):
        hook(reason)


def lifecycle_owners(bot: Bot) -> set[int]:
    """Identify forwarded bound hooks so two seats cannot share one session."""
    owners: set[int] = set()
    identity = getattr(bot, "lifecycle_identity", None)
    if callable(identity):
        owners.add(id(identity()))
    for name in ("start", "set_decision_context", "settle_decision", "cancel", "close"):
        hook = getattr(bot, name, None)
        if callable(hook):
            owners.add(id(getattr(hook, "__self__", bot)))
    return owners
