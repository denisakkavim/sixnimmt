"""Optional, short local hooks for bots owning a match session or external work."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from sixnimmt.arena.bots.base import Bot, DelegatingBot, Rejection
from sixnimmt.arena.results import MatchOutcome
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.views import MatchView


class ControllerStopped(Exception):
    """An operator stopped the match while a bot was waiting for a decision."""


class DecisionDeadlineExceeded(TimeoutError):
    """A bot exhausted its decision budget before the outer deadline."""


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


@runtime_checkable
class StartingBot(Protocol):
    def start(self, context: BotContext) -> None: ...


@runtime_checkable
class DeadlineBot(Protocol):
    def set_decision_context(self, context: DecisionContext) -> None: ...


@runtime_checkable
class SettlingBot(Protocol):
    def settle_decision(self, outcome: DecisionOutcome) -> None: ...


@runtime_checkable
class CancellingBot(Protocol):
    def cancel(self, reason: str) -> None: ...


@runtime_checkable
class ClosingBot(Protocol):
    def close(self, result: BotMatchEnd | None) -> None: ...


@runtime_checkable
class ResourceBot(Protocol):
    def lifecycle_identity(self) -> object: ...


def start_bot(bot: Bot, context: BotContext) -> None:
    if isinstance(bot, StartingBot):
        bot.start(context)
    elif isinstance(bot, DelegatingBot):
        start_bot(bot.delegate_bot(), context)


def close_bot(bot: Bot, result: BotMatchEnd | None) -> None:
    if isinstance(bot, ClosingBot):
        bot.close(result)
    elif isinstance(bot, DelegatingBot):
        close_bot(bot.delegate_bot(), result)


def set_decision_context(bot: Bot, context: DecisionContext) -> None:
    if isinstance(bot, DeadlineBot):
        bot.set_decision_context(context)
    elif isinstance(bot, DelegatingBot):
        set_decision_context(bot.delegate_bot(), context)


def settle_decision(bot: Bot, outcome: DecisionOutcome) -> None:
    if isinstance(bot, SettlingBot):
        bot.settle_decision(outcome)
    elif isinstance(bot, DelegatingBot):
        settle_decision(bot.delegate_bot(), outcome)


def cancel_bot(bot: Bot, reason: str) -> None:
    if isinstance(bot, CancellingBot):
        bot.cancel(reason)
    elif isinstance(bot, DelegatingBot):
        cancel_bot(bot.delegate_bot(), reason)


def lifecycle_owners(bot: Bot) -> set[int]:
    """Identify all resources in a delegate chain so seats cannot share a session."""
    owners: set[int] = set()
    if isinstance(bot, ResourceBot):
        owners.add(id(bot.lifecycle_identity()))
    if isinstance(bot, (StartingBot, DeadlineBot, SettlingBot, CancellingBot, ClosingBot)):
        owners.add(id(bot))
    if isinstance(bot, DelegatingBot):
        owners.update(lifecycle_owners(bot.delegate_bot()))
    return owners
