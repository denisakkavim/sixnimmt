"""Available arena strategies and their construction registry."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue

from sixnimmt.arena.bots.base import ActionBatch, Bot, BotOptions, BotSpec, Rejection
from sixnimmt.arena.bots.composed import (
    CandidateRanking,
    ControlledBurnBot,
    ControlledBurnOptions,
    CountThresholdBaitBot,
    CountThresholdBaitOptions,
    HandAwareRowChoiceBot,
    HandAwareRowChoiceOptions,
    resolve_composed,
)
from sixnimmt.arena.bots.heuristics import (
    ClosestGapBot,
    ColdestRowBot,
    HandFlexibilityBot,
    HighestCardBot,
    HighestFittingCardBot,
    LowestCardBot,
    LowestFittingCardBot,
    RandomBot,
)
from sixnimmt.arena.bots.llm import LLMBot, LLMMemoryBot, LLMMemoryOptions, LLMOptions
from sixnimmt.arena.bots.simulation import ModelBasedBaitBot, SimulationBot, resolve_simulation
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


@dataclass(frozen=True)
class _SeedOnlyConstructor:
    build: Callable[[int], Bot]

    def __call__(self, seed: int, options: BotOptions) -> Bot:
        return self.build(seed)


def _baseline(name: str, build: Callable[[int], Bot], version: str = "1") -> BotSpec:
    return BotSpec.typed(name, BotOptions, _SeedOnlyConstructor(build), True, {"strategy_id": name, "version": version})


def _construct_simulation(seed: int, options: SimulationOptions) -> SimulationBot:
    return SimulationBot(seed, options)


def _construct_llm(seed: int, options: LLMOptions) -> LLMBot:
    return LLMBot(seed, validated_options=options)


def _construct_memory_llm(seed: int, options: LLMMemoryOptions) -> LLMMemoryBot:
    return LLMMemoryBot(seed, validated_options=options)


REGISTRY: dict[str, BotSpec] = {
    "simulation": BotSpec.typed(
        "simulation", SimulationOptions, _construct_simulation, True, {"strategy_id": "simulation", "version": "1"}
    ),
    "model_based_bait": BotSpec(
        "model_based_bait",
        build_model_based_bait,
        False,
        {"strategy_id": "model_based_bait", "version": "1"},
        ModelBasedBaitOptions,
        resolve_simulation,
    ),
    "hand_aware_row_choice": BotSpec(
        "hand_aware_row_choice",
        build_hand_aware_row_choice,
        False,
        {"strategy_id": "hand_aware_row_choice", "version": "1"},
        HandAwareRowChoiceOptions,
        resolve_composed,
    ),
    "count_threshold_bait": BotSpec(
        "count_threshold_bait",
        build_count_threshold_bait,
        False,
        {"strategy_id": "count_threshold_bait", "version": "1"},
        CountThresholdBaitOptions,
        resolve_composed,
    ),
    "controlled_burn": BotSpec(
        "controlled_burn",
        build_controlled_burn,
        False,
        {"strategy_id": "controlled_burn", "version": "1"},
        ControlledBurnOptions,
        resolve_composed,
    ),
    "hand_flexibility": _baseline("hand_flexibility", _build_hand_flexibility),
    "highest_fitting_card": _baseline("highest_fitting_card", _build_highest_fitting_card),
    "coldest_row": _baseline("coldest_row", _build_coldest_row),
    "closest_gap": _baseline("closest_gap", _build_closest_gap),
    "highest_card": _baseline("highest_card", _build_highest_card),
    "lowest_card": _baseline("lowest_card", _build_lowest_card),
    "llm": BotSpec.typed("llm", LLMOptions, _construct_llm, False, {"strategy_id": "llm", "version": "1"}),
    "llm_memory": BotSpec.typed(
        "llm_memory", LLMMemoryOptions, _construct_memory_llm, False, {"strategy_id": "llm_memory", "version": "1"}
    ),
    "random": _baseline("random", RandomBot, "2"),
    "lowest_fitting_card": _baseline("lowest_fitting_card", _build_lowest_fitting_card),
}
