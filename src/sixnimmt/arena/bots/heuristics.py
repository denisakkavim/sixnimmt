"""Heuristics bot implementations and their supporting types."""

import random

from sixnimmt.arena.bots.base import Bot, Rejection
from sixnimmt.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.cards import bull_heads
from sixnimmt.engine.views import MatchView, RowView


def cheapest_row(rows: tuple[RowView, ...]) -> RowView:
    return min(rows, key=lambda row: (row_penalty(row), row.index))


def applicable_row(card: int, rows: tuple[RowView, ...]) -> RowView | None:
    lower_rows = [row for row in rows if row.cards[-1] < card]
    if len(lower_rows) == 0:
        return None
    return max(lower_rows, key=lambda row: row.cards[-1])


def currently_fits(card: int, rows: tuple[RowView, ...]) -> bool:
    row = applicable_row(card, rows)
    if row is None:
        return False
    # Fit is evaluated before opponents' cards change the board.
    return len(row.cards) < 5


def immediate_penalty(card: int, rows: tuple[RowView, ...]) -> int:
    row = applicable_row(card, rows)
    if row is None:
        # Below every row end, this bot chooses the cheapest row to take.
        return min(row_penalty(candidate_row) for candidate_row in rows)
    if len(row.cards) == 5:
        return row_penalty(row)
    return 0


def row_penalty(row: RowView) -> int:
    return sum(bull_heads(card) for card in row.cards)


class RandomBot(Bot):
    """Choose cards uniformly with a private RNG and always take the cheapest row."""

    def __init__(self, seed: int) -> None:
        self._random = random.Random(seed)  # noqa: S311 - reproducible game choices

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if "choose_row" in view.legal_actions:
            row = cheapest_row(view.rows)
            return ChooseRowAction(row_index=row.index)
        if "commit" in view.legal_actions:
            return CommitAction()
        return SelectCardAction(card=self._random.choice(view.you.hand))


class LowestCardBot(Bot):
    """Play the lowest card and take the cheapest row when required."""

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if "choose_row" in view.legal_actions:
            row = cheapest_row(view.rows)
            return ChooseRowAction(row_index=row.index)
        if "commit" in view.legal_actions:
            return CommitAction()
        return SelectCardAction(card=min(view.you.hand))


class HighestCardBot(Bot):
    """Play the highest card and take the cheapest row when required."""

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if "choose_row" in view.legal_actions:
            row = cheapest_row(view.rows)
            return ChooseRowAction(row_index=row.index)
        if "commit" in view.legal_actions:
            return CommitAction()
        return SelectCardAction(card=max(view.you.hand))


class LowestFittingCardBot(Bot):
    """Play the lowest fitting card; otherwise minimise cost, then card value."""

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if "choose_row" in view.legal_actions:
            row = cheapest_row(view.rows)
            return ChooseRowAction(row_index=row.index)
        if "commit" in view.legal_actions:
            return CommitAction()

        fitting_cards = [card for card in view.you.hand if currently_fits(card, view.rows)]
        if len(fitting_cards) > 0:
            return SelectCardAction(card=min(fitting_cards))

        card = min(view.you.hand, key=lambda card: (immediate_penalty(card, view.rows), card))
        return SelectCardAction(card=card)


class HighestFittingCardBot(Bot):
    """Play the highest fitting card; otherwise minimise cost, then card value."""

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if "choose_row" in view.legal_actions:
            row = cheapest_row(view.rows)
            return ChooseRowAction(row_index=row.index)
        if "commit" in view.legal_actions:
            return CommitAction()

        fitting_cards = [card for card in view.you.hand if currently_fits(card, view.rows)]
        if len(fitting_cards) > 0:
            return SelectCardAction(card=max(fitting_cards))

        card = min(view.you.hand, key=lambda card: (immediate_penalty(card, view.rows), card))
        return SelectCardAction(card=card)


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

        if len(fitting_candidates) > 0:
            _, card = min(fitting_candidates)
            return SelectCardAction(card=card)

        card = min(view.you.hand, key=lambda card: (immediate_penalty(card, view.rows), card))
        return SelectCardAction(card=card)


class ColdestRowBot(Bot):
    """Minimise fitting row occupancy, then card value; fall back to pickup cost."""

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
            occupancy = len(row.cards)
            fitting_candidates.append((occupancy, card))

        if len(fitting_candidates) > 0:
            _, card = min(fitting_candidates)
            return SelectCardAction(card=card)

        card = min(view.you.hand, key=lambda card: (immediate_penalty(card, view.rows), card))
        return SelectCardAction(card=card)


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
