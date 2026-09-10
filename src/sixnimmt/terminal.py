"""Terminal progress and entertainment while the arena runs."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import monotonic

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from sixnimmt.arena.players import PlayerConfig
from sixnimmt.engine.cards import bull_heads

_CARDS = (5, 10, 11, 20, 55, 66)
_TIPS = (
    "55 carries seven bull heads. A tiny card with a big attitude.",
    "The sixth card takes the five before it. Timing is everything.",
    "Too low for every row? You get to choose your own misfortune.",
    "The lowest score wins. Let somebody else collect the cattle.",
)
_CAPTIONS = (
    "A peaceful row. What could possibly go wrong?",
    "Just one little card...",
    "There's room for another.",
    "The herd is getting restless.",
    "Four cards. Still perfectly fine.",
    "Five cards. The bull would like a word.",
    "6 nimmt! [66] takes Row 1: +20 bull heads!",
    "The bull keeps the five. 66 starts a fresh row.",
)


def _board(stage: int) -> Table:
    # A fictional deal, with four nonempty rows and at most five cards each.
    # The incoming sixth card is announced separately before replacing its row.
    first_row = (66,) if stage == 7 else _CARDS[: max(1, min(stage, 5))]
    rows = [first_row, (70,), (80, 83), (100,)]
    if stage >= 2:
        rows[1] += (72,)
    if stage >= 3:
        rows[2] += (89,)
    if stage >= 4:
        rows[1] += (74,)
    if stage >= 5:
        rows[3] += (104,)
    table = Table.grid(padding=(0, 1))
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    table.add_column(justify="right", no_wrap=True)
    for index, cards in enumerate(rows, start=1):
        style = "bold red" if stage == 6 and index == 1 else "cyan"
        slots = Text()
        for slot in range(5):
            if slot > 0:
                slots.append(" ")
            if slot < len(cards):
                slots.append(f"[{cards[slot]:3}]", style=style)
            else:
                slots.append("[ · ]", style="dim")
        heads = sum(bull_heads(card) for card in cards)
        table.add_row(str(index), slots, Text(f"{heads} ^", style=style))
    return table


def _player_table(names: tuple[str, ...], tick: int) -> Table:
    table = Table.grid(padding=(0, 1))
    table.add_column(justify="right", style="dim", no_wrap=True)
    table.add_column(overflow="fold")
    table.add_column(no_wrap=True)
    active = tick % len(names) if names else None
    for index, name in enumerate(names):
        selected = index == active
        style = "bold yellow" if selected else "dim"
        stage = tick % len(_CAPTIONS)
        card_index = max(0, min(stage - 1, len(_CARDS) - 1))
        card = f"[ {_CARDS[card_index]:3} ]" if selected else "[  ?  ]"
        table.add_row(f"{index + 1}.", Text(name, style=style), Text(card, style=style))
    return table


def _animation_frame(
    elapsed: float, games: int, seed: int, width: int, names: tuple[str, ...], completed: int = 0
) -> Panel:
    stage = int(elapsed / 0.8) % len(_CAPTIONS)
    seconds = int(elapsed)
    minutes, seconds = divmod(seconds, 60)
    bull = "(OO)" if stage == 6 else "(oo)"
    status = Text(f"  \\ /   Running {games:,} matches · Seed {seed}\n  {bull}  Elapsed {minutes:02}:{seconds:02}")
    status.stylize("bold yellow", 0, 7)
    tip = _TIPS[int(elapsed / 8) % len(_TIPS)]
    return Panel(
        Group(
            status,
            Text(f"Matches completed: {completed:,}/{games:,} · {completed / games:.0%}"),
            ProgressBar(total=games, completed=completed),
            Text(),
            Text(f"{len(names)} players at the doodle table", style="bold"),
            _player_table(names, int(elapsed / 0.8)),
            Text(),
            Text("Four rows · five slots · ^ bull heads", style="dim"),
            _board(stage),
            Text(_CAPTIONS[stage], style="bold yellow" if stage == 6 else "cyan"),
            Text(),
            Text(tip, style="dim"),
        ),
        title="6 nimmt! · The bull pen",
        subtitle="Table doodle · while the bots play",
        border_style="yellow" if stage == 6 else "cyan",
        width=min(width, 76),
    )


@dataclass
class _ArenaAnimation:
    console: Console
    games: int
    seed: int
    names: tuple[str, ...]
    started: float = field(default_factory=monotonic)
    completed: int = 0

    def update(self, completed: int) -> None:
        self.completed = completed

    def render(self) -> Panel:
        return _animation_frame(
            monotonic() - self.started, self.games, self.seed, self.console.width, self.names, self.completed
        )


@contextmanager
def arena_animation(
    games: int, seed: int, players: Sequence[PlayerConfig], *, enabled: bool = True
) -> Iterator[_ArenaAnimation | None]:
    """Animate only on interactive terminals; always restore the display on exit."""
    console = Console(highlight=False)
    if games < 1 or not enabled or not console.is_terminal or console.is_dumb_terminal:
        yield None
        return
    names: list[str] = []
    for seat, player in enumerate(players, start=1):
        name = player.display_name
        if name is None:
            name = f"Player {seat}"
        elif name == "":
            name = f"player_{seat}"
        names.append(name)
    animation = _ArenaAnimation(console, games, seed, tuple(names))
    with Live(console=console, get_renderable=animation.render, refresh_per_second=4, transient=True):
        yield animation
