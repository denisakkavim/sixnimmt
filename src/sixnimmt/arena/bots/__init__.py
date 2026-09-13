"""Available arena strategies and their construction registry."""

from typing import Any

from pydantic import JsonValue

from sixnimmt.arena.bots.base import ActionBatch, Bot, BotOptions, BotSpec, Rejection
from sixnimmt.arena.bots.closest_gap import ClosestGapBot
from sixnimmt.arena.bots.coldest_row import ColdestRowBot
from sixnimmt.arena.bots.controlled_burn import ControlledBurnBot, ControlledBurnOptions
from sixnimmt.arena.bots.count_threshold_bait import (
    CandidateRanking,
    CountThresholdBaitBot,
    CountThresholdBaitOptions,
)
from sixnimmt.arena.bots.hand_aware_row_choice import (
    HandAwareRowChoiceBot,
    HandAwareRowChoiceOptions,
)
from sixnimmt.arena.bots.hand_flexibility import HandFlexibilityBot
from sixnimmt.arena.bots.highest_card import HighestCardBot
from sixnimmt.arena.bots.highest_fitting_card import HighestFittingCardBot
from sixnimmt.arena.bots.llm import LLMBot, LLMOptions
from sixnimmt.arena.bots.llm_memory import LLMMemoryBot, LLMMemoryOptions
from sixnimmt.arena.bots.lowest_card import LowestCardBot
from sixnimmt.arena.bots.lowest_fitting_card import LowestFittingCardBot
from sixnimmt.arena.bots.random import RandomBot
from sixnimmt.arena.bots.simulation import ModelBasedBaitBot, SimulationBot, build_simulation
from sixnimmt.arena.bots.uncertainty.options import ModelBasedBaitOptions, SimulationOptions

__all__ = [
    "REGISTRY",
    "ActionBatch",
    "Bot",
    "BotOptions",
    "BotSpec",
    "ClosestGapBot",
    "ColdestRowBot",
    "ControlledBurnBot",
    "ControlledBurnOptions",
    "CountThresholdBaitBot",
    "CountThresholdBaitOptions",
    "HandAwareRowChoiceBot",
    "HandAwareRowChoiceOptions",
    "HandFlexibilityBot",
    "HighestCardBot",
    "HighestFittingCardBot",
    "LLMBot",
    "LLMMemoryBot",
    "LLMMemoryOptions",
    "LLMOptions",
    "LowestCardBot",
    "LowestFittingCardBot",
    "ModelBasedBaitOptions",
    "RandomBot",
    "Rejection",
    "SimulationBot",
    "SimulationOptions",
]


def _build_hand_flexibility(seed: int) -> HandFlexibilityBot:
    return HandFlexibilityBot()


def _build_highest_fitting_card(seed: int) -> HighestFittingCardBot:
    return HighestFittingCardBot()


def _build_coldest_row(seed: int) -> ColdestRowBot:
    return ColdestRowBot()


def _build_closest_gap(seed: int) -> ClosestGapBot:
    return ClosestGapBot()


def _build_highest_card(seed: int) -> HighestCardBot:
    return HighestCardBot()


def _build_lowest_card(seed: int) -> LowestCardBot:
    return LowestCardBot()


def _build_lowest_fitting_card(seed: int) -> LowestFittingCardBot:
    return LowestFittingCardBot()


def _build_registered(seed: int, name: str, settings: dict[str, JsonValue]) -> Bot:
    spec = REGISTRY[name]
    options = spec.options_model.model_validate(settings)
    return spec.build(seed, **options.model_dump())


def build_controlled_burn(
    seed: int, *, K: int, fallback_strategy: str, fallback_options: dict[str, JsonValue] | None = None
) -> ControlledBurnBot:
    fallback = _build_registered(seed, fallback_strategy, fallback_options if fallback_options is not None else {})
    return ControlledBurnBot(K, fallback)


def build_count_threshold_bait(
    seed: int,
    *,
    intervening_card_threshold: int,
    candidate_ranking: CandidateRanking,
    fallback_strategy: str,
    fallback_options: dict[str, JsonValue] | None = None,
) -> CountThresholdBaitBot:
    fallback = _build_registered(seed, fallback_strategy, fallback_options if fallback_options is not None else {})
    return CountThresholdBaitBot(intervening_card_threshold, candidate_ranking, fallback)


def build_hand_aware_row_choice(
    seed: int, *, max_extra_penalty: int, card_strategy: str, card_options: dict[str, JsonValue] | None = None
) -> HandAwareRowChoiceBot:
    card_bot = _build_registered(seed, card_strategy, card_options if card_options is not None else {})
    return HandAwareRowChoiceBot(max_extra_penalty, card_bot)


def build_model_based_bait(seed: int, **settings: Any) -> ModelBasedBaitBot:
    options = ModelBasedBaitOptions.model_validate(settings)
    fallback = _build_registered(seed, options.fallback_strategy, options.fallback_options)
    return ModelBasedBaitBot(seed, options, fallback)


REGISTRY: dict[str, BotSpec] = {
    "simulation": BotSpec(
        "simulation", build_simulation, True, {"strategy_id": "simulation", "version": "1"}, SimulationOptions
    ),
    "model_based_bait": BotSpec(
        "model_based_bait",
        build_model_based_bait,
        False,
        {"strategy_id": "model_based_bait", "version": "1"},
        ModelBasedBaitOptions,
    ),
    "hand_aware_row_choice": BotSpec(
        "hand_aware_row_choice",
        build_hand_aware_row_choice,
        False,
        {"strategy_id": "hand_aware_row_choice", "version": "1"},
        HandAwareRowChoiceOptions,
    ),
    "count_threshold_bait": BotSpec(
        "count_threshold_bait",
        build_count_threshold_bait,
        False,
        {"strategy_id": "count_threshold_bait", "version": "1"},
        CountThresholdBaitOptions,
    ),
    "controlled_burn": BotSpec(
        "controlled_burn",
        build_controlled_burn,
        False,
        {"strategy_id": "controlled_burn", "version": "1"},
        ControlledBurnOptions,
    ),
    "hand_flexibility": BotSpec(
        "hand_flexibility", _build_hand_flexibility, True, {"strategy_id": "hand_flexibility", "version": "1"}
    ),
    "highest_fitting_card": BotSpec(
        "highest_fitting_card",
        _build_highest_fitting_card,
        True,
        {"strategy_id": "highest_fitting_card", "version": "1"},
    ),
    "coldest_row": BotSpec("coldest_row", _build_coldest_row, True, {"strategy_id": "coldest_row", "version": "1"}),
    "closest_gap": BotSpec("closest_gap", _build_closest_gap, True, {"strategy_id": "closest_gap", "version": "1"}),
    "highest_card": BotSpec("highest_card", _build_highest_card, True, {"strategy_id": "highest_card", "version": "1"}),
    "lowest_card": BotSpec("lowest_card", _build_lowest_card, True, {"strategy_id": "lowest_card", "version": "1"}),
    "llm": BotSpec("llm", LLMBot, False, {"strategy_id": "llm", "version": "1"}, LLMOptions),
    "llm_memory": BotSpec(
        "llm_memory", LLMMemoryBot, False, {"strategy_id": "llm_memory", "version": "1"}, LLMMemoryOptions
    ),
    "random": BotSpec("random", RandomBot, True, {"strategy_id": "random", "version": "2"}),
    "lowest_fitting_card": BotSpec(
        "lowest_fitting_card",
        _build_lowest_fitting_card,
        True,
        {"strategy_id": "lowest_fitting_card", "version": "1"},
    ),
}
