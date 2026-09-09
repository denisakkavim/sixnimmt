"""Retain distributed hand coverage among equally cheap immediate plays."""

from sixnimmt.arena.bots._board import cheapest_row, immediate_penalty
from sixnimmt.arena.bots.base import Bot, Rejection
from sixnimmt.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.views import MatchView


class HandFlexibilityBot(Bot):
    """Minimise immediate cost, remaining-hand coverage distance, then card value."""

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if "choose_row" in view.legal_actions:
            row = cheapest_row(view.rows)
            return ChooseRowAction(row_index=row.index)
        if "commit" in view.legal_actions:
            return CommitAction()

        hand = view.you.hand
        if len(hand) == 1:
            return SelectCardAction(card=hand[0])

        candidates: list[tuple[int, float, int]] = []
        for card in hand:
            penalty = immediate_penalty(card, view.rows)
            remaining_hand = tuple(held_card for held_card in hand if held_card != card)
            coverage_distance = _coverage_distance(remaining_hand)
            candidates.append((penalty, coverage_distance, card))

        _, _, card = min(candidates)
        return SelectCardAction(card=card)


def _coverage_distance(hand: tuple[int, ...]) -> float:
    """Average distance from every deck value to the nearest retained card."""
    # Include seen values too: coverage is fixed, not an opponent-card belief.
    total_distance = sum(min(abs(value - card) for card in hand) for value in range(1, 105))
    return total_distance / 104
