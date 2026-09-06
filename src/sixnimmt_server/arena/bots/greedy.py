"""Arena strategy minimising immediate visible penalties."""

from sixnimmt_server.arena.bots.base import Bot, Rejection
from sixnimmt_server.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt_server.engine.cards import bull_heads
from sixnimmt_server.engine.views import MatchView, RowView


class GreedyBot(Bot):
    """Minimise immediate visible penalty, breaking ties by card or row index."""

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if "choose_row" in view.legal_actions:
            row = min(view.rows, key=lambda row: (sum(bull_heads(card) for card in row.cards), row.index))
            return ChooseRowAction(row_index=row.index)
        if "commit" in view.legal_actions:
            return CommitAction()

        card = min(view.you.hand, key=lambda card: (_immediate_penalty(card, view.rows), card))
        return SelectCardAction(card=card)


def _immediate_penalty(card: int, rows: tuple[RowView, ...]) -> int:
    eligible = [row for row in rows if row.cards[-1] < card]
    if not eligible:
        return min(sum(bull_heads(card) for card in row.cards) for row in rows)
    row = max(eligible, key=lambda row: row.cards[-1])
    return sum(bull_heads(card) for card in row.cards) if len(row.cards) == 5 else 0
