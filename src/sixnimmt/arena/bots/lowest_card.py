"""Board-independent lowest card baseline."""

from sixnimmt.arena.bots._board import cheapest_row
from sixnimmt.arena.bots.base import Bot, Rejection
from sixnimmt.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.views import MatchView


class LowestCardBot(Bot):
    """Play the lowest card and take the cheapest row when required."""

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if "choose_row" in view.legal_actions:
            row = cheapest_row(view.rows)
            return ChooseRowAction(row_index=row.index)
        if "commit" in view.legal_actions:
            return CommitAction()
        return SelectCardAction(card=min(view.you.hand))
