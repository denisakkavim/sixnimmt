"""Current-board evaluations shared by the baseline strategies."""

from sixnimmt.engine.cards import bull_heads
from sixnimmt.engine.views import RowView


def cheapest_row(rows: tuple[RowView, ...]) -> RowView:
    return min(rows, key=lambda row: (row_penalty(row), row.index))


def applicable_row(card: int, rows: tuple[RowView, ...]) -> RowView | None:
    lower_rows = [row for row in rows if row.cards[-1] < card]
    if not lower_rows:
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
