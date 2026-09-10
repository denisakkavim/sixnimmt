"""Per-seat bot configuration and validated construction settings."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from sixnimmt.arena.bots import REGISTRY, Bot, BotSpec
from sixnimmt.arena.bots.controlled_burn import ControlledBurnBot, ControlledBurnOptions
from sixnimmt.arena.bots.count_threshold_bait import CandidateRanking, CountThresholdBaitBot, CountThresholdBaitOptions
from sixnimmt.arena.bots.hand_aware_row_choice import HandAwareRowChoiceBot, HandAwareRowChoiceOptions
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


def _build_controlled_burn(
    seed: int, *, K: int, fallback_strategy: str, fallback_options: dict[str, Any], fallback: ResolvedPlayer
) -> ControlledBurnBot:
    # The resolved factory travels to process workers; their registry may differ.
    return ControlledBurnBot(K, fallback.build(seed))


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
        build_options = options.model_dump()
        recorded_options = options.model_dump(mode="json")
        if isinstance(options, (ControlledBurnOptions, CountThresholdBaitOptions)):
            fallback = resolve_players([PlayerConfig(bot=options.fallback_strategy, options=options.fallback_options)])[
                0
            ]
            builder = (
                _build_controlled_burn if isinstance(options, ControlledBurnOptions) else _build_count_threshold_bait
            )
            spec = replace(
                spec,
                build=partial(builder, fallback=fallback),
                deterministic=fallback.deterministic,
                metadata={**spec.metadata, "fallback_metadata": fallback.metadata},
            )
            recorded_options["fallback_options"] = fallback.recorded_options
            build_options["fallback_options"] = fallback.options
        elif isinstance(options, HandAwareRowChoiceOptions):
            card_player = PlayerConfig(bot=options.card_strategy, options=options.card_options)
            card_strategy = resolve_players([card_player])[0]
            spec = replace(
                spec,
                build=partial(_build_hand_aware_row_choice, card_strategy_resolved=card_strategy),
                deterministic=card_strategy.deterministic,
                metadata={**spec.metadata, "card_strategy_metadata": card_strategy.metadata},
            )
            recorded_options["card_options"] = card_strategy.recorded_options
            build_options["card_options"] = card_strategy.options
        resolved.append(ResolvedPlayer(player.model_copy(deep=True), spec, build_options, recorded_options))
    return resolved


def _build_count_threshold_bait(
    seed: int,
    *,
    intervening_card_threshold: int,
    candidate_ranking: CandidateRanking,
    fallback_strategy: str,
    fallback_options: dict[str, Any],
    fallback: ResolvedPlayer,
) -> CountThresholdBaitBot:
    return CountThresholdBaitBot(intervening_card_threshold, candidate_ranking, fallback.build(seed))


def _build_hand_aware_row_choice(
    seed: int,
    *,
    max_extra_penalty: int,
    card_strategy: str,
    card_options: dict[str, Any],
    card_strategy_resolved: ResolvedPlayer,
) -> HandAwareRowChoiceBot:
    return HandAwareRowChoiceBot(max_extra_penalty, card_strategy_resolved.build(seed))
