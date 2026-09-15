"""Shared card rows for live games and decorative table animations."""

from collections.abc import Sequence

from rich.table import Table
from rich.text import Text

from sixnimmt.engine.cards import bull_heads


def render_board(
    rows: Sequence[Sequence[int]],
    *,
    highlighted_row: int | None = None,
    highlighted_card: int | None = None,
    captured: bool = False,
) -> Table:
    """Render supplied rows with five slots, penalty totals, and optional highlights."""
    table = Table.grid(padding=(0, 1))
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    table.add_column(justify="right", no_wrap=True)
    for index, row in enumerate(rows):
        highlighted = index == highlighted_row
        style = "bold red" if highlighted and captured else "cyan"
        cards = Text()
        for slot in range(5):
            if slot > 0:
                cards.append(" ")
            if slot >= len(row):
                cards.append("[ · ]", style="dim")
                continue
            card = row[slot]
            card_style = "bold black on yellow" if highlighted and card == highlighted_card else style
            cards.append(f"[{card:3}]", style=card_style)
        heads = sum(bull_heads(card) for card in row)
        table.add_row(str(index + 1), cards, Text(f"{heads} ^", style=style))
    return table
