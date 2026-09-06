"""Available arena strategies and their construction registry."""

from sixnimmt_server.arena.bots.base import Bot, BotOptions, BotSpec, Rejection
from sixnimmt_server.arena.bots.greedy import GreedyBot
from sixnimmt_server.arena.bots.llm import LLMBot, LLMOptions
from sixnimmt_server.arena.bots.llm_memory import LLMMemoryBot, LLMMemoryOptions
from sixnimmt_server.arena.bots.random import RandomBot

__all__ = [
    "REGISTRY",
    "Bot",
    "BotOptions",
    "BotSpec",
    "GreedyBot",
    "LLMBot",
    "LLMMemoryBot",
    "LLMMemoryOptions",
    "LLMOptions",
    "RandomBot",
    "Rejection",
]


REGISTRY: dict[str, BotSpec] = {
    "llm": BotSpec("llm", LLMBot, False, {"strategy_id": "llm", "version": "1"}, LLMOptions),
    "llm_memory": BotSpec(
        "llm_memory", LLMMemoryBot, False, {"strategy_id": "llm_memory", "version": "1"}, LLMMemoryOptions
    ),
    "random": BotSpec("random", RandomBot, True, {"strategy_id": "random", "version": "1"}),
    "greedy": BotSpec("greedy", lambda seed: GreedyBot(), True, {"strategy_id": "greedy", "version": "1"}),
}
