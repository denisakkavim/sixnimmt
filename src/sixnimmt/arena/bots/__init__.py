"""Available arena strategies and their construction registry."""

from sixnimmt.arena.bots.base import ActionBatch, Bot, BotOptions, BotSpec, Rejection
from sixnimmt.arena.bots.closest_gap import ClosestGapBot
from sixnimmt.arena.bots.coldest_row import ColdestRowBot
from sixnimmt.arena.bots.controlled_burn import ControlledBurnBot, ControlledBurnOptions, build_controlled_burn
from sixnimmt.arena.bots.count_threshold_bait import (
    CountThresholdBaitBot,
    CountThresholdBaitOptions,
    build_count_threshold_bait,
)
from sixnimmt.arena.bots.hand_flexibility import HandFlexibilityBot
from sixnimmt.arena.bots.highest_card import HighestCardBot
from sixnimmt.arena.bots.highest_fitting_card import HighestFittingCardBot
from sixnimmt.arena.bots.llm import LLMBot, LLMOptions
from sixnimmt.arena.bots.llm_memory import LLMMemoryBot, LLMMemoryOptions
from sixnimmt.arena.bots.lowest_card import LowestCardBot
from sixnimmt.arena.bots.lowest_fitting_card import LowestFittingCardBot
from sixnimmt.arena.bots.random import RandomBot

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
    "HandFlexibilityBot",
    "HighestCardBot",
    "HighestFittingCardBot",
    "LLMBot",
    "LLMMemoryBot",
    "LLMMemoryOptions",
    "LLMOptions",
    "LowestCardBot",
    "LowestFittingCardBot",
    "RandomBot",
    "Rejection",
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


REGISTRY: dict[str, BotSpec] = {
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
