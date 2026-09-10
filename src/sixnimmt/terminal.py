"""Terminal progress and entertainment while the arena runs."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from random import Random
from time import monotonic

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from sixnimmt.arena.players import PlayerConfig
from sixnimmt.engine.cards import bull_heads

_TIPS = (
    "55 carries seven bull heads. A tiny card with a big attitude.",
    "The sixth card takes the five before it. Timing is everything.",
    "Too low for every row? You get to choose your own misfortune.",
    "The lowest score wins. Let somebody else collect the cattle.",
)
_PLAYS_PER_DEAL = 30


@dataclass(frozen=True)
class _DoodlePlay:
    rows: tuple[tuple[int, ...], ...]
    card: int
    seat: int
    target: int
    takes_row: bool

    @property
    def heads(self) -> int:
        return sum(bull_heads(card) for card in self.rows[self.target])

    def placed_rows(self) -> tuple[tuple[int, ...], ...]:
        rows = list(self.rows)
        rows[self.target] = (self.card,) if self.takes_row else (*rows[self.target], self.card)
        return tuple(rows)


@lru_cache(maxsize=1)
def _doodle_deal(seed: int, deal: int, players: int) -> tuple[_DoodlePlay, ...]:
    # This private RNG never consumes the arena's random streams.
    rng = Random(f"bull-pen:{seed}:{deal}")  # noqa: S311 - decorative, reproducible deals
    deck = rng.sample(range(1, 105), 104)
    rows = tuple((card,) for card in deck[:4])
    plays: list[_DoodlePlay] = []
    seats = list(range(max(1, players)))
    for turn, card in enumerate(deck[4 : 4 + _PLAYS_PER_DEAL]):
        if turn % len(seats) == 0:
            rng.shuffle(seats)
        eligible = [index for index, row in enumerate(rows) if row[-1] < card]
        target = max(eligible, key=lambda index: rows[index][-1]) if eligible else rng.randrange(4)
        play = _DoodlePlay(rows, card, seats[turn % len(seats)], target, not eligible or len(rows[target]) == 5)
        plays.append(play)
        rows = play.placed_rows()
    return tuple(plays)


def _caption(play: _DoodlePlay, placed: bool) -> str:
    if play.takes_row:
        reason = "Too low!" if play.card < min(row[-1] for row in play.rows) else "6 nimmt!"
        return f"{reason} [{play.card}] takes Row {play.target + 1}: +{play.heads} bull heads!"
    verb = "joins" if placed else "heads for"
    return f"[{play.card}] {verb} Row {play.target + 1}. The herd grows..."


def _board(play: _DoodlePlay, placed: bool) -> Table:
    rows = play.placed_rows() if placed else play.rows
    table = Table.grid(padding=(0, 1))
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    table.add_column(justify="right", no_wrap=True)
    for index, cards in enumerate(rows, start=1):
        style = "bold red" if play.takes_row and index == play.target + 1 else "cyan"
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


def _player_table(names: tuple[str, ...], play: _DoodlePlay) -> Table:
    table = Table.grid(padding=(0, 1))
    table.add_column(justify="right", style="dim", no_wrap=True)
    table.add_column(overflow="fold")
    table.add_column(no_wrap=True)
    for index, name in enumerate(names):
        selected = index == play.seat
        style = "bold yellow" if selected else "dim"
        card = f"[ {play.card:3} ]" if selected else "[  ?  ]"
        table.add_row(f"{index + 1}.", Text(name, style=style), Text(card, style=style))
    return table


def _animation_frame(
    elapsed: float, games: int, seed: int, width: int, names: tuple[str, ...], completed: int = 0
) -> Panel:
    tick = int(elapsed / 0.8)
    deal, frame = divmod(tick, _PLAYS_PER_DEAL * 2)
    play = _doodle_deal(seed, deal, len(names))[frame // 2]
    placed = frame % 2 == 1
    seconds = int(elapsed)
    minutes, seconds = divmod(seconds, 60)
    bull = "(OO)" if play.takes_row else "(oo)"
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
            _player_table(names, play),
            Text(),
            Text("Four rows · five slots · ^ bull heads", style="dim"),
            _board(play, placed),
            Text(_caption(play, placed), style="bold yellow" if play.takes_row else "cyan"),
            Text(),
            Text(tip, style="dim"),
        ),
        title="6 nimmt! · The bull pen",
        subtitle="Table doodle · while the bots play",
        border_style="yellow" if play.takes_row else "cyan",
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
