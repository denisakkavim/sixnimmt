"""Prefer the smallest gap to a currently fitting row."""

from sixnimmt.arena.bots._board import applicable_row, cheapest_row, immediate_penalty
from sixnimmt.arena.bots.base import Bot, Rejection
from sixnimmt.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.views import MatchView


class ClosestGapBot(Bot):
    """Minimise fitting gap, then card value; fall back to minimum pickup cost."""

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if "choose_row" in view.legal_actions:
            row = cheapest_row(view.rows)
            return ChooseRowAction(row_index=row.index)
        if "commit" in view.legal_actions:
            return CommitAction()

        fitting_candidates: list[tuple[int, int]] = []
        for card in view.you.hand:
            row = applicable_row(card, view.rows)
            if row is None or len(row.cards) == 5:
                continue
            gap = card - row.cards[-1]
            fitting_candidates.append((gap, card))

        if fitting_candidates:
            _, card = min(fitting_candidates)
            return SelectCardAction(card=card)

        card = min(view.you.hand, key=lambda card: (immediate_penalty(card, view.rows), card))
        return SelectCardAction(card=card)
