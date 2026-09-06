"""Seeded random arena strategy."""

import random

from sixnimmt.arena.bots.base import Bot, Rejection
from sixnimmt.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.views import MatchView


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
