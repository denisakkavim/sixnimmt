from sixnimmt.arena.bots.base import Bot
from sixnimmt.arena.bots.composed import ControlledBurnBot, CountThresholdBaitBot, HandAwareRowChoiceBot
from sixnimmt.arena.bots.external import HarnessBot, ManagedHarnessBot
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
from sixnimmt.arena.bots.llm import LLMBot, LLMMemoryBot
from sixnimmt.arena.bots.simulation import ModelBasedBaitBot, SimulationBot


def test_shipped_bot_classes_inherit_from_bot_protocol() -> None:
    bot_classes = (
        ClosestGapBot,
        ColdestRowBot,
        ControlledBurnBot,
        CountThresholdBaitBot,
        HandAwareRowChoiceBot,
        HandFlexibilityBot,
        HarnessBot,
        HighestCardBot,
        HighestFittingCardBot,
        LLMBot,
        LLMMemoryBot,
        LowestCardBot,
        LowestFittingCardBot,
        ManagedHarnessBot,
        ModelBasedBaitBot,
        RandomBot,
        SimulationBot,
    )

    for bot_class in bot_classes:
        assert Bot in bot_class.__mro__, f"{bot_class.__name__} does not inherit from Bot"
