"""Shared bot contracts and validated options."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Concatenate, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, JsonValue

from sixnimmt.engine.actions import Action
from sixnimmt.engine.errors import ErrorCode
from sixnimmt.engine.views import MatchView

if TYPE_CHECKING:
    from sixnimmt.arena.players import ResolvedStrategy


MAX_BATCH_OPERATIONS = 8


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


@runtime_checkable
class MemoryBot(Protocol):
    """Accept preflighted memory synchronously, without provider calls."""

    def accept_batch(self, batch: ActionBatch) -> None: ...


@runtime_checkable
class StatisticsBot(Protocol):
    def stats(self) -> dict[str, Any]: ...


@runtime_checkable
class TracedBot(Protocol):
    def set_trace(self, callback: Callable[[dict[str, Any]], None] | None) -> None: ...


@runtime_checkable
class ObservingBot(Protocol):
    """Ingest a filtered observation without choosing a move or calling a provider."""

    def observe(self, view: MatchView) -> None: ...


@runtime_checkable
class DelegatingBot(Protocol):
    """Expose a delegate explicitly, without claiming arbitrary attributes exist."""

    def delegate_bot(self) -> Bot: ...


def observe_bot(bot: Bot, view: MatchView) -> None:
    if isinstance(bot, ObservingBot):
        bot.observe(view)
    elif isinstance(bot, DelegatingBot):
        observe_bot(bot.delegate_bot(), view)


def memory_bot(bot: Bot) -> MemoryBot | None:
    if isinstance(bot, MemoryBot):
        return bot
    if isinstance(bot, DelegatingBot):
        return memory_bot(bot.delegate_bot())
    return None


def statistics_bot(bot: Bot) -> StatisticsBot | None:
    if isinstance(bot, StatisticsBot):
        return bot
    if isinstance(bot, DelegatingBot):
        return statistics_bot(bot.delegate_bot())
    return None


def traced_bot(bot: Bot) -> TracedBot | None:
    if isinstance(bot, TracedBot):
        return bot
    if isinstance(bot, DelegatingBot):
        return traced_bot(bot.delegate_bot())
    return None


@dataclass(frozen=True)
class BotSpec:
    """A registered strategy; build receives a seed and validated option keywords."""

    name: str
    build: Callable[Concatenate[int, ...], Bot]
    deterministic: bool
    metadata: dict[str, Any]
    options_model: type[BotOptions] = BotOptions
    resolve: Callable[[BotOptions, "ResolveStrategy"], "StrategyConstruction"] | None = None

    @classmethod
    def typed[OptionsT: BotOptions](
        cls,
        name: str,
        options_model: type[OptionsT],
        constructor: Callable[[int, OptionsT], Bot],
        deterministic: bool,
        metadata: dict[str, JsonValue],
    ) -> "BotSpec":
        """Register a constructor whose option type is checked with its schema."""
        factory = TypedStrategyFactory(options_model, constructor, deterministic)
        return cls(name, factory, deterministic, metadata, options_model, factory.resolve)


class ResolveStrategy(Protocol):
    def __call__(self, name: str, options: dict[str, JsonValue], /) -> "ResolvedStrategy": ...


@dataclass(frozen=True)
class StrategyConstruction:
    """A picklable factory with resolved delegate provenance."""

    build: Callable[[int], Bot]
    deterministic: bool
    metadata: dict[str, Any] = field(default_factory=dict)
    recorded_options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _BoundTypedFactory[OptionsT: BotOptions]:
    constructor: Callable[[int, OptionsT], Bot]
    options: OptionsT

    def __call__(self, seed: int) -> Bot:
        return self.constructor(seed, self.options.model_copy(deep=True))


@dataclass(frozen=True)
class TypedStrategyFactory[OptionsT: BotOptions]:
    """A precise construction path with an isolated legacy keyword adapter."""

    options_model: type[OptionsT]
    constructor: Callable[[int, OptionsT], Bot]
    deterministic: bool

    def __call__(self, seed: int, **settings: Any) -> Bot:
        # Existing REGISTRY[name].build(seed, **options) callers remain supported.
        return self.constructor(seed, self.options_model.model_validate(settings))

    def resolve(self, options: BotOptions, resolve: ResolveStrategy) -> StrategyConstruction:
        if not isinstance(options, self.options_model):
            msg = "validated options do not match the registered strategy schema"
            raise TypeError(msg)
        return StrategyConstruction(_BoundTypedFactory(self.constructor, options), self.deterministic)
