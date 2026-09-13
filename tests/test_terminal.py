"""Checks for the decorative terminal animation."""

import random

import pytest
from rich.console import Console

from sixnimmt.arena.players import PlayerConfig
from sixnimmt.engine.cards import bull_heads
from sixnimmt.terminal import (
    _analysis_frame,
    _animation_frame,
    _board,
    _caption,
    _doodle_deal,
    _statistics_draw,
    analysis_animation,
    arena_animation,
)


def test_doodle_capture_caption_matches_the_cards_taken() -> None:
    for play in _doodle_deal(66, 0, 5):
        if play.takes_row:
            heads = sum(bull_heads(card) for card in play.rows[play.target])
            assert f"Row {play.target + 1}: +{heads} bull heads!" in _caption(play, True)
            assert play.placed_rows()[play.target] == (play.card,)


def test_doodle_varies_rows_capture_totals_and_played_cards() -> None:
    deals = [_doodle_deal(66, deal, 5) for deal in range(8)]
    captures = [play for deal in deals for play in deal if play.takes_row]
    assert {play.target for play in captures} == {0, 1, 2, 3}
    assert len({play.heads for play in captures}) > 5
    assert len({tuple(play.card for play in deal) for deal in deals}) == len(deals)
    assert len({tuple(play.seat for play in deal) for deal in deals}) == len(deals)


def test_doodle_cards_follow_placement_rules() -> None:
    for play in _doodle_deal(66, 0, 5):
        eligible = [index for index, row in enumerate(play.rows) if row[-1] < play.card]
        if len(eligible) > 0:
            assert play.target == max(eligible, key=lambda index: play.rows[index][-1])
            assert play.takes_row == (len(play.rows[play.target]) == 5)
        else:
            assert play.takes_row
        cards = [card for row in play.placed_rows() for card in row]
        assert len(cards) == len(set(cards))


@pytest.mark.parametrize("width", [40, 54, 80])
def test_animation_fits_terminal_width(width: int) -> None:
    console = Console(width=width, color_system=None)
    with console.capture() as capture:
        console.print(_animation_frame(4.9, 100, 1234, width, ("Alice", "Bob")))
    output = capture.get()
    assert all(len(line) <= width for line in output.splitlines())
    assert "Elapsed 00:04" in output


def test_animation_restores_cursor_after_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("TTY_COMPATIBLE", "1")
    monkeypatch.setenv("TERM", "xterm")
    with pytest.raises(KeyboardInterrupt), arena_animation(10, 1234, [PlayerConfig(bot="random")] * 2):
        raise KeyboardInterrupt
    output = capsys.readouterr().out
    assert "\x1b[?25h" in output


@pytest.mark.parametrize("count", [2, 5, 10])
def test_doodle_shows_every_configured_seat(count: int) -> None:
    names = tuple(f"Bot {index + 1}" for index in range(count))
    console = Console(width=80, color_system=None)
    with console.capture() as capture:
        console.print(_animation_frame(0, 10, 1234, 80, names))
    output = capture.get()
    assert f"{count} players at the doodle table" in output
    for name in names:
        assert name in output


@pytest.mark.parametrize("placed", [False, True])
def test_doodle_always_has_four_nonempty_rows_with_five_slots(placed: bool) -> None:
    console = Console(width=80, color_system=None)
    for play in _doodle_deal(66, 0, 5):
        with console.capture() as capture:
            console.print(_board(play, placed))
        rows = capture.get().splitlines()
        assert len(rows) == 4
        for index, row in enumerate(rows, start=1):
            assert row.startswith(str(index))
            assert row.count("[") == 5
            assert row.count("[ · ]") < 5


@pytest.mark.parametrize("completed", [0, 5, 10])
def test_progress_displays_completed_matches(completed: int) -> None:
    console = Console(width=80, color_system=None)
    with console.capture() as capture:
        console.print(_animation_frame(2, 10, 66, 80, ("Alice", "Bob"), completed))
    assert f"Matches completed: {completed}/10 · {completed / 10:.0%}" in capture.get()


@pytest.mark.parametrize("width", [32, 40, 54, 80])
def test_analysis_animation_fits_width_and_labels_toy_statistics(width: int) -> None:
    console = Console(width=width, color_system=None)
    with console.capture() as capture:
        console.print(_analysis_frame(64.9, 1000, 11, 66, width))
    output = capture.get()
    assert all(len(line) <= width for line in output.splitlines())
    assert "Elapsed 01:04" in output
    assert "1,000 games" in output
    assert "11 strategies" in output
    assert "Decorative toy draw" in output


def test_analysis_doodle_varies_cards_and_resampled_histogram() -> None:
    console = Console(width=80, color_system=None)
    frames: list[str] = []
    for elapsed in (0.0, 3.0, 8.0, 14.0):
        with console.capture() as capture:
            console.print(_analysis_frame(elapsed, 1000, 11, 66, 80))
        frames.append(capture.get())
    assert len(set(frames)) == 4
    assert _statistics_draw(66, 0) != _statistics_draw(66, 1)
    assert "Toy data, not arena results." in frames[0]
    assert "%" not in frames[0]


def test_statistics_doodle_does_not_consume_shared_randomness() -> None:
    before = random.getstate()
    first = _statistics_draw(1234, 2)
    _analysis_frame(10.0, 100, 11, 1234, 80)
    assert random.getstate() == before
    assert _statistics_draw(1234, 2) == first


def test_analysis_animation_restores_cursor_after_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("TTY_COMPATIBLE", "1")
    monkeypatch.setenv("TERM", "xterm")
    with pytest.raises(KeyboardInterrupt), analysis_animation(10, 11, 66):
        raise KeyboardInterrupt
    output = capsys.readouterr().out
    assert "\x1b[?25h" in output


@pytest.mark.parametrize(
    ("enabled", "terminal", "term"), [(False, "1", "xterm"), (True, "0", "xterm"), (True, "1", "dumb")]
)
def test_analysis_animation_stays_silent_when_disabled_or_noninteractive(
    monkeypatch: pytest.MonkeyPatch, capsys, enabled: bool, terminal: str, term: str
) -> None:
    monkeypatch.setenv("TTY_COMPATIBLE", terminal)
    monkeypatch.setenv("TERM", term)
    with analysis_animation(10, 11, 66, enabled=enabled) as animation:
        assert animation is None
    assert capsys.readouterr().out == ""
