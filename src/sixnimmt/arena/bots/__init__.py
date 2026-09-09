"""Available arena strategies and their construction registry."""

from sixnimmt.arena.bots.base import ActionBatch, Bot, BotOptions, BotSpec, Rejection
from sixnimmt.arena.bots.closest_gap import ClosestGapBot
from sixnimmt.arena.bots.coldest_row import ColdestRowBot
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


REGISTRY: dict[str, BotSpec] = {
    "hand_flexibility": BotSpec(
        "hand_flexibility", lambda seed: HandFlexibilityBot(), True, {"strategy_id": "hand_flexibility", "version": "1"}
    ),
    "highest_fitting_card": BotSpec(
        "highest_fitting_card",
        lambda seed: HighestFittingCardBot(),
        True,
        {"strategy_id": "highest_fitting_card", "version": "1"},
    ),
    "coldest_row": BotSpec(
        "coldest_row", lambda seed: ColdestRowBot(), True, {"strategy_id": "coldest_row", "version": "1"}
    ),
    "closest_gap": BotSpec(
        "closest_gap", lambda seed: ClosestGapBot(), True, {"strategy_id": "closest_gap", "version": "1"}
    ),
    "highest_card": BotSpec(
        "highest_card", lambda seed: HighestCardBot(), True, {"strategy_id": "highest_card", "version": "1"}
    ),
    "lowest_card": BotSpec(
        "lowest_card", lambda seed: LowestCardBot(), True, {"strategy_id": "lowest_card", "version": "1"}
    ),
    "llm": BotSpec("llm", LLMBot, False, {"strategy_id": "llm", "version": "1"}, LLMOptions),
    "llm_memory": BotSpec(
        "llm_memory", LLMMemoryBot, False, {"strategy_id": "llm_memory", "version": "1"}, LLMMemoryOptions
    ),
    "random": BotSpec("random", RandomBot, True, {"strategy_id": "random", "version": "2"}),
    "lowest_fitting_card": BotSpec(
        "lowest_fitting_card",
        lambda seed: LowestFittingCardBot(),
        True,
        {"strategy_id": "lowest_fitting_card", "version": "1"},
    ),
}
