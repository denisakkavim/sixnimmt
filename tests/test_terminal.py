"""Checks for the decorative terminal animation."""

import pytest
from rich.console import Console

from sixnimmt.arena.players import PlayerConfig
from sixnimmt.terminal import _animation_frame, _board, arena_animation


@pytest.mark.parametrize(
    "elapsed, caption, card",
    [
        (4.1, "Five cards. The bull would like a word.", "55"),
        (4.9, "6 nimmt! [66] takes Row 1: +20 bull heads!", "66"),
        (5.7, "The bull keeps the five. 66 starts a fresh row.", "66"),
    ],
)
def test_doodle_shows_the_sixth_card_taking_the_row(elapsed: float, caption: str, card: str) -> None:
    console = Console(width=80, color_system=None)
    with console.capture() as capture:
        console.print(_animation_frame(elapsed, 10, 1234, 80, ("Alice", "Bob")))
    output = capture.get()
    assert caption in output
    assert card in output
    assert "Table doodle" in output


@pytest.mark.parametrize("width", [40, 54, 80])
def test_animation_fits_terminal_width(width: int) -> None:
    console = Console(width=width, color_system=None)
    with console.capture() as capture:
        console.print(_animation_frame(4.9, 100, 1234, width, ("Alice", "Bob")))
    output = capture.get()
    assert all(len(line) <= width for line in output.splitlines())
    assert "66" in output
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


@pytest.mark.parametrize("stage", range(8))
def test_doodle_always_has_four_nonempty_rows_with_five_slots(stage: int) -> None:
    console = Console(width=80, color_system=None)
    with console.capture() as capture:
        console.print(_board(stage))
    rows = capture.get().splitlines()
    assert len(rows) == 4
    for index, row in enumerate(rows, start=1):
        assert row.startswith(str(index))
        assert row.count("[") == 5
        assert row.count("[ · ]") < 5
