"""Shared bot contracts and validated options."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict

from sixnimmt.engine.actions import Action
from sixnimmt.engine.errors import ErrorCode
from sixnimmt.engine.views import MatchView


class BotOptions(BaseModel):
    """Reject unsupported keys and preserve explicit configuration types."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


@dataclass(frozen=True)
class Rejection:
    """The last refusal, alongside the refreshed view supplied to a retry."""

    code: ErrorCode
    message: str
    legal_actions: tuple[str, ...]
    action: Action | None = None


@dataclass(frozen=True)
class ActionBatch:
    """One atomic proposal; memory is private and optional (None preserves it)."""

    actions: tuple[Action, ...]
    memory: str | None = field(default=None, repr=False)

    @property
    def size(self) -> int:
        return len(self.actions) + int(self.memory is not None)


class Bot(Protocol):
    """One instance per match. Atomic batches are validated before publication."""

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action | ActionBatch: ...


class MemoryBot(Protocol):
    """Accept preflighted memory synchronously, without provider calls."""

    def accept_batch(self, batch: ActionBatch) -> None: ...


class StatisticsBot(Protocol):
    def stats(self) -> dict[str, Any]: ...


class TracedBot(Protocol):
    def set_trace(self, callback: Callable[[dict[str, Any]], None] | None) -> None: ...


def memory_bot(bot: Bot) -> MemoryBot | None:
    # Delegate bots forward optional capabilities through __getattr__.
    return cast(MemoryBot, bot) if callable(getattr(bot, "accept_batch", None)) else None


def statistics_bot(bot: Bot) -> StatisticsBot | None:
    return cast(StatisticsBot, bot) if callable(getattr(bot, "stats", None)) else None


def traced_bot(bot: Bot) -> TracedBot | None:
    return cast(TracedBot, bot) if callable(getattr(bot, "set_trace", None)) else None


@dataclass(frozen=True)
class BotSpec:
    """A registered strategy; build receives a seed and validated option keywords."""

    name: str
    build: Callable[..., Bot]
    deterministic: bool
    metadata: dict[str, Any]
    options_model: type[BotOptions] = BotOptions
