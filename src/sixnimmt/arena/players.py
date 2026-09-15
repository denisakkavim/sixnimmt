"""Per-seat bot configuration and validated construction settings."""

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, field_validator

from sixnimmt.arena.bots.base import Bot, BotOptions, BotSpec
from sixnimmt.arena.bots.registry import REGISTRY
from sixnimmt.common.text import check_representable


class PlayerConfig(BaseModel):
    """A registered bot type, its options, and the identity of one arena seat."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    bot: str = Field(min_length=1)
    display_name: str | None = None
    options: dict[str, JsonValue] = Field(default_factory=dict)
    agent_metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("display_name")
    @classmethod
    def check_name(cls, value: str | None) -> str | None:
        check_representable(value)
        return value


_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True)
class ResolvedStrategy:
    """Validated strategy construction and provenance, independent of a seat."""

    spec: BotSpec
    options: BotOptions
    recorded_options: dict[str, JsonValue]
    factory: Callable[[int], Bot]

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def deterministic(self) -> bool:
        return self.spec.deterministic

    @property
    def metadata(self) -> dict[str, JsonValue]:
        metadata = _JSON_OBJECT.validate_python(deepcopy(self.spec.metadata))
        metadata["bot_options"] = deepcopy(self.recorded_options)
        return metadata

    def build(self, seed: int) -> Bot:
        return self.factory(seed)


@dataclass(frozen=True)
class ResolvedPlayer:
    """A resolved strategy with the caller's seat identity and annotations."""

    config: PlayerConfig
    strategy: ResolvedStrategy

    @property
    def spec(self) -> BotSpec:
        return self.strategy.spec

    @property
    def options(self) -> BotOptions:
        return self.strategy.options

    @property
    def recorded_options(self) -> dict[str, JsonValue]:
        return self.strategy.recorded_options

    @property
    def factory(self) -> Callable[[int], Bot]:
        return self.strategy.factory

    @property
    def name(self) -> str:
        return self.strategy.name

    @property
    def deterministic(self) -> bool:
        return self.strategy.deterministic

    @property
    def metadata(self) -> dict[str, JsonValue]:
        metadata = {**self.strategy.metadata, **deepcopy(self.config.agent_metadata)}
        metadata["bot_options"] = deepcopy(self.recorded_options)
        return metadata

    def build(self, seed: int) -> Bot:
        return self.strategy.build(seed)


@dataclass(frozen=True)
class _LegacyFactory:
    """Compatibility boundary for keyword-based third-party BotSpec factories."""

    spec: BotSpec
    options: BotOptions

    def __call__(self, seed: int) -> Bot:
        # model_dump creates fresh nested values for every match.
        return self.spec.build(seed, **self.options.model_dump())


def resolve_players(players: Sequence[PlayerConfig]) -> list[ResolvedPlayer]:
    resolved = []
    for index, player in enumerate(players):
        if not isinstance(player, PlayerConfig):
            msg = "players must contain PlayerConfig objects, not bot names"
            raise TypeError(msg)
        try:
            strategy = resolve_strategy(player.bot, player.options)
        except ValueError as error:
            msg = f"invalid options for player {index + 1} ({player.bot}): {error}"
            raise ValueError(msg) from error
        resolved.append(ResolvedPlayer(player.model_copy(deep=True), strategy))
    return resolved


def resolve_strategy(name: str, settings: dict[str, JsonValue]) -> ResolvedStrategy:
    """Resolve nested strategies without manufacturing an anonymous player seat."""
    if name not in REGISTRY:
        msg = f"unknown bot {name!r}; available bots: {', '.join(sorted(REGISTRY))}"
        raise ValueError(msg)
    spec = REGISTRY[name]
    options = spec.options_model.model_validate(deepcopy(settings))
    recorded_options = _JSON_OBJECT.validate_python(options.model_dump(mode="json"))
    factory: Callable[[int], Bot] = _LegacyFactory(spec, options)
    if spec.resolve is not None:
        construction = spec.resolve(options, resolve_strategy)
        factory = construction.build
        spec = replace(
            spec, deterministic=construction.deterministic, metadata={**spec.metadata, **construction.metadata}
        )
        recorded_options.update(_JSON_OBJECT.validate_python(construction.recorded_options))
    _JSON_OBJECT.validate_python(spec.metadata)
    return ResolvedStrategy(spec, options, recorded_options, factory)
