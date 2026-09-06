"""Per-seat bot configuration and validated construction settings."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from sixnimmt.arena.bots import REGISTRY, Bot, BotSpec
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
    options: dict[str, Any]
    recorded_options: dict[str, Any]

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
        return self.spec.build(seed, **deepcopy(self.options))


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
        resolved.append(
            ResolvedPlayer(player.model_copy(deep=True), spec, options.model_dump(), options.model_dump(mode="json"))
        )
    return resolved
