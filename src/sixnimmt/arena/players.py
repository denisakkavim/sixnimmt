"""Per-seat bot configuration and validated construction settings."""

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

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


@dataclass(frozen=True)
class ResolvedPlayer:
    config: PlayerConfig
    spec: BotSpec
    options: BotOptions
    recorded_options: dict[str, Any]
    factory: Callable[[int], Bot] | None = None

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def deterministic(self) -> bool:
        return self.spec.deterministic

    @property
    def metadata(self) -> dict[str, Any]:
        metadata = {**deepcopy(self.spec.metadata), **deepcopy(self.config.agent_metadata)}
        metadata["bot_options"] = deepcopy(self.recorded_options)
        return metadata

    def build(self, seed: int) -> Bot:
        # A factory may mutate a nested option; no other game or seat shares it.
        if self.factory is not None:
            return self.factory(seed)
        return self.spec.build(seed, **self.options.model_dump())


def resolve_players(players: Sequence[PlayerConfig]) -> list[ResolvedPlayer]:
    resolved = []
    for index, player in enumerate(players):
        if not isinstance(player, PlayerConfig):
            msg = "players must contain PlayerConfig objects, not bot names"
            raise TypeError(msg)
        if player.bot not in REGISTRY:
            msg = f"unknown bot {player.bot!r}; available bots: {', '.join(sorted(REGISTRY))}"
            raise ValueError(msg)
        spec = REGISTRY[player.bot]
        try:
            options = spec.options_model.model_validate(deepcopy(player.options))
        except ValueError as error:
            msg = f"invalid options for player {index + 1} ({player.bot}): {error}"
            raise ValueError(msg) from error
        recorded_options = options.model_dump(mode="json")
        factory = None
        if spec.resolve is not None:
            construction = spec.resolve(options, _resolve_strategy)
            factory = construction.build
            spec = replace(
                spec, deterministic=construction.deterministic, metadata={**spec.metadata, **construction.metadata}
            )
            recorded_options.update(construction.recorded_options)
        resolved.append(ResolvedPlayer(player.model_copy(deep=True), spec, options, recorded_options, factory))
    return resolved


def _resolve_strategy(name: str, options: dict[str, JsonValue]) -> ResolvedPlayer:
    return resolve_players([PlayerConfig(bot=name, options=options)])[0]
