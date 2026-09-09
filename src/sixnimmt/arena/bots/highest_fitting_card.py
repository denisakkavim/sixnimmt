"""Highest currently fitting card strategy with minimum-cost fallback."""

from sixnimmt.arena.bots._board import cheapest_row, currently_fits, immediate_penalty
from sixnimmt.arena.bots.base import Bot, Rejection
from sixnimmt.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.views import MatchView


class HighestFittingCardBot(Bot):
    """Play the highest fitting card; otherwise minimise cost, then card value."""

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if "choose_row" in view.legal_actions:
            row = cheapest_row(view.rows)
            return ChooseRowAction(row_index=row.index)
        if "commit" in view.legal_actions:
            return CommitAction()

        fitting_cards = [card for card in view.you.hand if currently_fits(card, view.rows)]
        if fitting_cards:
            return SelectCardAction(card=max(fitting_cards))

        card = min(view.you.hand, key=lambda card: (immediate_penalty(card, view.rows), card))
        return SelectCardAction(card=card)
