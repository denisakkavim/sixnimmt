"""Shared bot contracts and validated options."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from sixnimmt_server.engine.actions import Action
from sixnimmt_server.engine.errors import ErrorCode
from sixnimmt_server.engine.views import MatchView


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


@dataclass(frozen=True)
class BotSpec:
    """A registered strategy; build receives a seed and validated option keywords."""

    name: str
    build: Callable[..., Bot]
    deterministic: bool
    metadata: dict[str, Any]
    options_model: type[BotOptions] = BotOptions
