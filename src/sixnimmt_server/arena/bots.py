"""Trusted bot callbacks and the registry of available strategies."""

import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sixnimmt_server.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt_server.engine.cards import bull_heads
from sixnimmt_server.engine.errors import ErrorCode
from sixnimmt_server.engine.views import MatchView


@dataclass(frozen=True)
class Rejection:
    """The last refusal, alongside the refreshed view supplied to a retry."""

    code: ErrorCode
    message: str
    legal_actions: tuple[str, ...]


class Bot(Protocol):
    """One instance per match. An optional stats() method may report JSON data."""

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action: ...


@dataclass(frozen=True)
class BotSpec:
    name: str
    build: Callable[[int], Bot]
    deterministic: bool
    metadata: dict[str, Any]


class RandomBot(Bot):
    """Choose uniformly from the visible hand or rows using a private RNG."""

    def __init__(self, seed: int) -> None:
        self._random = random.Random(seed)  # noqa: S311 - reproducible game choices

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if "choose_row" in view.legal_actions:
            return ChooseRowAction(row_index=self._random.choice(view.rows).index)
        if "commit" in view.legal_actions:
            return CommitAction()
        return SelectCardAction(card=self._random.choice(view.you.hand))


class GreedyBot(Bot):
    """Minimise immediate visible penalty, breaking ties by card or row index."""

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if "choose_row" in view.legal_actions:
            row = min(view.rows, key=lambda row: (sum(bull_heads(card) for card in row.cards), row.index))
            return ChooseRowAction(row_index=row.index)
        if "commit" in view.legal_actions:
            return CommitAction()

        def cost(card: int) -> tuple[int, int]:
            eligible = [row for row in view.rows if row.cards[-1] < card]
            if not eligible:
                return min(sum(bull_heads(card) for card in row.cards) for row in view.rows), card
            row = max(eligible, key=lambda row: row.cards[-1])
            return (sum(bull_heads(card) for card in row.cards) if len(row.cards) == 5 else 0), card

        return SelectCardAction(card=min(view.you.hand, key=cost))


REGISTRY: dict[str, BotSpec] = {
    "random": BotSpec("random", RandomBot, True, {"strategy_id": "random", "version": "1"}),
    "greedy": BotSpec("greedy", lambda seed: GreedyBot(), True, {"strategy_id": "greedy", "version": "1"}),
}
