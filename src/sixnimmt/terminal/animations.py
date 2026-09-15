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
from sixnimmt.terminal.board import render_board

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
        target = max(eligible, key=lambda index: rows[index][-1]) if len(eligible) > 0 else rng.randrange(4)
        play = _DoodlePlay(rows, card, seats[turn % len(seats)], target, len(eligible) == 0 or len(rows[target]) == 5)
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
    return render_board(rows, highlighted_row=play.target, captured=play.takes_row)


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


_STATISTICS_TIPS = (
    "The bull is checking its figures. Twice, just to be herd.",
    "Tied leaders share the win. Even the bull knows how to divide.",
    "Same opponents, same deal: a fair test for a new strategy.",
    "A tall toy bar is no victory. The real results are still cooking.",
)
_STATISTICS_DRAW_SIZE = 24


@lru_cache(maxsize=1)
def _statistics_draw(seed: int, cycle: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    # Decorative resampling must never change game or analysis randomness.
    rng = Random(f"bull-statistics:{seed}:{cycle}")  # noqa: S311 - independent decorative randomness
    available = [card for card in range(1, 105) if card != 55]
    chosen = rng.sample(available, 5)
    chosen.append(55)
    cards = tuple(sorted(chosen))
    draw = tuple(rng.choice(cards) for _ in range(_STATISTICS_DRAW_SIZE))
    return cards, draw


def _statistics_histogram(draw: tuple[int, ...], width: int) -> Table:
    table = Table.grid(padding=(0, 1))
    table.add_column(no_wrap=True)
    table.add_column()
    table.add_column(justify="right", no_wrap=True)
    bar_width = max(4, min(24, width - 20))
    current_heads = bull_heads(draw[-1])
    for heads in (1, 2, 3, 5, 7):
        count = sum(bull_heads(card) == heads for card in draw)
        length = max(1, round(count * bar_width / _STATISTICS_DRAW_SIZE)) if count > 0 else 0
        style = "bold yellow" if heads == current_heads else "cyan"
        bar = Text("^" * length, style=style)
        bar.append("·" * (bar_width - length), style="dim")
        label = "1 head" if heads == 1 else f"{heads} heads"
        table.add_row(Text(label, style=style), bar, Text(str(count), style=style))
    return table


def _analysis_frame(elapsed: float, games: int, strategies: int, seed: int, width: int) -> Panel:
    tick = int(elapsed / 0.45)
    cycle, position = divmod(tick, _STATISTICS_DRAW_SIZE)
    cards, draw = _statistics_draw(seed, cycle)
    sample = draw[: position + 1]
    eyes = ("(oo)", "(o-)", "(oo)", "(-o)")[tick % 4]
    minutes, seconds = divmod(int(elapsed), 60)
    status = Text(f"  \\ /   Calculating statistics\n  {eyes}  Elapsed {minutes:02}:{seconds:02}\n   vv")
    status.stylize("bold magenta", 0, 7)
    card_strip = Text("Toy cards: ", style="dim")
    for card in cards:
        card_strip.append(f"[{card:3}] ", style="bold yellow" if card == sample[-1] else "cyan")
    tip = _STATISTICS_TIPS[int(elapsed / 6) % len(_STATISTICS_TIPS)]
    return Panel(
        Group(
            status,
            Text(f"{games:,} games · {strategies:,} strategies"),
            Text("Toy data, not arena results.", style="dim"),
            Text(),
            card_strip,
            Text(f"Resampling toy cards · last draw [{sample[-1]}]", style="bold"),
            _statistics_histogram(sample, width),
            Text(),
            Text(tip, style="dim"),
        ),
        title="6 nimmt! · The statistics stable",
        subtitle="Decorative toy draw",
        border_style="magenta",
        width=min(width, 76),
    )


@dataclass
class _AnalysisAnimation:
    console: Console
    games: int
    strategies: int
    seed: int
    started: float = field(default_factory=monotonic)

    def render(self) -> Panel:
        return _analysis_frame(monotonic() - self.started, self.games, self.strategies, self.seed, self.console.width)


@contextmanager
def analysis_animation(
    games: int, strategies: int, seed: int, *, enabled: bool = True
) -> Iterator[_AnalysisAnimation | None]:
    """Entertain during analysis without implying a completion rate or measured performance."""
    console = Console(highlight=False)
    if games < 0 or strategies < 1 or not enabled or not console.is_terminal or console.is_dumb_terminal:
        yield None
        return
    animation = _AnalysisAnimation(console, games, strategies, seed)
    with Live(console=console, get_renderable=animation.render, refresh_per_second=4, transient=True):
        yield animation
