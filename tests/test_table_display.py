"""Actual public event animation and isolated operator activity."""

from datetime import UTC, datetime, timedelta
from threading import Event as ThreadEvent
from typing import Any

import pytest
from rich.console import Console

from sixnimmt.engine.events import (
    CardPlacedEvent,
    CardsRevealedEvent,
    Event,
    HandEndedEvent,
    MatchAbandonedEvent,
    MessageSentEvent,
    PlayStartedEvent,
    RowsInitialisedEvent,
    RowTakenEvent,
    SelectionMadeEvent,
    SelectionRegisteredEvent,
)
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.state import MatchState
from sixnimmt.terminal import PublicTableDisplay, table_display
from sixnimmt.terminal.table import _MAX_FRAMES, _safe_text


@pytest.fixture
def table_state() -> tuple[MatchState, tuple[Event, ...]]:
    state, events = create_match("display-test", ["alice", "bob"], match_seed=123)
    return state, tuple(events)


def _render(display: PublicTableDisplay, width: int = 100) -> str:
    console = Console(width=width, color_system=None)
    with console.capture() as capture:
        console.print(display.render(width))
    return capture.get()


def _capture_events() -> tuple[Event, ...]:
    return (
        RowsInitialisedEvent(
            match_id="display-test", audience="public", data={"rows": [[10, 11, 12, 13, 14], [40], [60], [80]]}
        ),
        CardsRevealedEvent(match_id="display-test", audience="public", data={"selections": {"alice": 15, "bob": 50}}),
        RowTakenEvent(
            match_id="display-test",
            audience="public",
            data={
                "player_id": "alice",
                "row": 0,
                "captured": [10, 11, 12, 13, 14],
                "heads": 11,
                "reason": "sixth_card",
            },
        ),
        CardPlacedEvent(match_id="display-test", audience="public", data={"card": 15, "row": 0, "row_cards": [15]}),
        CardPlacedEvent(match_id="display-test", audience="public", data={"card": 50, "row": 1, "row_cards": [40, 50]}),
    )


def test_public_display_ignores_authoritative_hands_and_private_events(table_state) -> None:
    state, events = table_state
    display = PublicTableDisplay(report=lambda text: None, commentary=False)
    display.observe(state, events)
    display.observe(
        state,
        (
            SelectionMadeEvent(
                match_id=state.match_id,
                audience="player:alice",
                data={"player_id": "alice", "card": state.players[0].hand[0]},
            ),
            MessageSentEvent(
                match_id=state.match_id,
                audience="player:alice",
                data={"from": "alice", "to": "bob", "visibility": "direct", "body": "PRIVATE HAND CONTENT"},
            ),
        ),
    )
    assert display.folder.view().you.hand == ()
    assert display.folder.view().you.selection is None
    assert "PRIVATE HAND CONTENT" not in _render(display)
    assert "Operator commentary" not in _render(display)


def test_selection_stays_face_down_until_public_reveal(table_state) -> None:
    state, events = table_state
    display = PublicTableDisplay(report=lambda text: None)
    display.observe(state, events)
    display.observe(
        state, (SelectionRegisteredEvent(match_id=state.match_id, audience="public", data={"player_id": "alice"}),)
    )
    assert "[ ? ] selected" in _render(display)
    display.observe(
        state,
        (
            CardsRevealedEvent(
                match_id=state.match_id, audience="public", data={"selections": {"alice": 15, "bob": 50}}
            ),
        ),
    )
    assert "[ 15] revealed" in _render(display)
    assert "[ 50] revealed" in _render(display)


def test_batched_resolution_has_distinct_capture_and_placement_frames(
    table_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, events = table_state
    display = PublicTableDisplay(report=lambda text: None)
    display.observe(state, events)
    display._queued = True
    display.observe(state, _capture_events())
    rendered: list[str] = []
    for tick in range(5):
        monkeypatch.setattr("sixnimmt.terminal.table.monotonic", lambda tick=tick: 100.0 + tick)
        rendered.append(_render(display))
    assert "[ 10] [ 11] [ 12] [ 13] [ 14]" in rendered[0]
    assert "Reveal: alice [15]" in rendered[1]
    assert "alice takes Row 1" in rendered[2]
    assert "[ 10] [ 11] [ 12] [ 13] [ 14]" in rendered[2]
    assert "[ 15] [ · ] [ · ] [ · ] [ · ]" in rendered[3]
    assert "[ 40] [ 50] [ · ] [ · ] [ · ]" in rendered[4]


def test_score_includes_unbanked_hand_and_does_not_double_count_at_hand_end(table_state) -> None:
    state, events = table_state
    display = PublicTableDisplay(report=lambda text: None)
    display.observe(state, events)
    display.observe(state, _capture_events())
    assert "11 (0+11)" in _render(display)
    display.observe(
        state,
        (
            HandEndedEvent(
                match_id=state.match_id,
                audience="public",
                data={"hand": 1, "hand_scores": {"alice": 11, "bob": 0}, "totals": {"alice": 11, "bob": 0}},
            ),
        ),
    )
    assert "11 (11+0)" in _render(display)
    assert "22 (11+11)" not in _render(display)


def test_public_messages_and_names_are_literal_text_without_terminal_controls(table_state) -> None:
    state, events = table_state
    messages: list[str] = []
    display = PublicTableDisplay(report=messages.append, commentary=False)
    display.observe(state, events)
    display.observe(
        state,
        (
            MessageSentEvent(
                match_id=state.match_id,
                audience="public",
                data={"from": "alice", "visibility": "table", "body": "[red]hello[/red]\x1b[2J\u202eevil"},
            ),
        ),
    )
    output = _render(display)
    assert "[red]hello[/red]evil" in output
    assert "Public table messages" in output
    assert "\x1b" not in "".join(messages)
    assert "\u202e" not in "".join(messages)


def test_operator_comments_are_separate_and_can_be_disabled(table_state) -> None:
    state, events = table_state
    display = PublicTableDisplay(report=lambda text: None)
    display.observe(state, events)
    display.activity({"type": "model_text", "player_id": "alice", "text": "My private card is 99."})
    output = _render(display)
    assert "Operator commentary · may include private information" in output
    assert "My private card is 99." in output
    assert display.folder.view().messages == ()
    hidden = PublicTableDisplay(report=lambda text: None, commentary=False)
    hidden.observe(state, events)
    hidden.activity({"type": "model_text", "player_id": "alice", "text": "My private card is 99."})
    assert "My private card is 99." not in _render(hidden)
    assert hidden._commentary.snapshot().current is None


def test_simulation_candidates_are_ordered_by_value_in_operator_pane(table_state) -> None:
    state, events = table_state
    display = PublicTableDisplay(report=lambda text: None)
    display.observe(state, events)
    display.activity({
        "type": "simulation_evaluation",
        "player_id": "alice",
        "candidate_values": {"99": 4.5, "20": 0.25, "30": 1.0},
    })
    output = _render(display)
    assert "Candidate value · lower is better" in output
    assert output.index("[20]") < output.index("[30]") < output.index("[99]")
    assert "0.25" in output
    assert "4.50" in output


def test_activity_shows_deciding_deadline_retry_and_final_failure(table_state) -> None:
    state, events = table_state
    display = PublicTableDisplay(report=lambda text: None)
    display.observe(state, events)
    deadline = (datetime.now(UTC) + timedelta(seconds=90)).isoformat()
    display.activity({"type": "decision_started", "player_id": "alice", "deadline": deadline})
    assert "deciding" in _render(display)
    assert "s left" in _render(display)
    display.activity({"type": "protocol_repair", "player_id": "alice", "message": "Invalid card"})
    assert "retrying" in _render(display)
    display.activity({
        "type": "decision_finished",
        "player_id": "alice",
        "status": "failed",
        "text": "No valid proposal",
    })
    display.observe(state, (MatchAbandonedEvent(match_id=state.match_id, audience="public"),))
    display.activity({"type": "match_finished", "outcome": "bot_failed", "reason": "No valid proposal"})
    output = _render(display)
    assert "failed" in output
    assert "Match abandoned" in output
    assert "Match bot_failed: No valid proposal" in output


def test_backlog_is_bounded_and_finishes_on_latest_public_state(table_state) -> None:
    state, events = table_state
    display = PublicTableDisplay(report=lambda text: None)
    display.observe(state, events)
    display._queued = True
    for play in range(1, 101):
        display.observe(
            state, (PlayStartedEvent(match_id=state.match_id, audience="public", data={"hand": 1, "play": play}),)
        )
    assert len(display._pending) == _MAX_FRAMES
    display._finish()
    assert "Play 100" in _render(display)
    assert len(display._pending) == 0


def test_plain_output_is_queued_and_does_not_block_match_callbacks(
    table_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TTY_COMPATIBLE", "0")
    state, events = table_state
    entered = ThreadEvent()
    release = ThreadEvent()
    observed = ThreadEvent()
    callback_returned: list[bool] = []

    def report(text: str) -> None:
        callback_returned.append(observed.wait(timeout=1.0))
        entered.set()
        release.wait(timeout=2.0)

    with table_display(report=report) as display:
        display.observe(state, events)
        observed.set()
        assert entered.wait(timeout=1.0)
        assert callback_returned[0]
        display.activity({"type": "decision_started", "player_id": "alice"})
        release.set()


def test_redirected_output_has_public_updates_activity_and_final_result(
    table_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TTY_COMPATIBLE", "0")
    state, events = table_state
    messages: list[str] = []
    with table_display(report=messages.append) as display:
        display.observe(state, events)
        display.activity({"type": "match_finished", "outcome": "bot_failed", "reason": "Invalid card"})
    output = "\n".join(messages)
    assert "Rows " in output
    assert "Scores " in output
    assert "Invalid card" in output
    assert "\x1b" not in output


def test_quiet_context_has_no_output_or_background_thread(table_state, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("TTY_COMPATIBLE", "1")
    state, events = table_state
    messages: list[str] = []
    with table_display(quiet=True, report=messages.append) as display:
        display.observe(state, events)
        display.activity({"type": "model_text", "text": "hidden"})
    assert messages == []
    assert capsys.readouterr().out == ""
    assert not display._queued


def test_terminal_stays_idle_during_waiting_room(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("TTY_COMPATIBLE", "1")
    monkeypatch.setenv("TERM", "xterm")
    with table_display():
        assert capsys.readouterr().out == ""
    assert capsys.readouterr().out == ""


def test_terminal_restores_cursor_and_keeps_final_failure_on_interrupt(
    table_state, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("TTY_COMPATIBLE", "1")
    monkeypatch.setenv("TERM", "xterm")
    state, events = table_state
    with pytest.raises(KeyboardInterrupt), table_display() as display:
        display.observe(state, events)
        display.observe(state, (MatchAbandonedEvent(match_id=state.match_id, audience="public"),))
        display.activity({"type": "match_finished", "outcome": "abandoned", "reason": "Operator stopped"})
        raise KeyboardInterrupt
    output = capsys.readouterr().out
    assert "\x1b[?25h" in output
    assert "Operator stopped" in output
    assert "Match abandoned" in output


@pytest.mark.parametrize("width", [40, 60, 100])
def test_live_game_frame_fits_terminal_width(table_state, width: int) -> None:
    state, events = table_state
    display = PublicTableDisplay(report=lambda text: None)
    display.observe(state, events)
    display.observe(state, _capture_events())
    output = _render(display, width)
    assert all(len(line) <= width for line in output.splitlines())


def test_streamed_operator_text_is_bounded_and_has_no_ansi_sequences() -> None:
    display = PublicTableDisplay(report=lambda text: None)
    record: dict[str, Any] = {"type": "model_text", "player_id": "alice", "text": "x" * 500, "delta": True}
    for _ in range(100):
        display.activity(record)
    current = display._commentary.snapshot().current
    assert current is not None
    assert len(current.messages) == 1
    assert len(current.messages[0].text) == 16000
    assert current.messages[0].truncated
    assert _safe_text("\x1b]52;c;SECRET\x07safe") == "safe"


def test_existing_llm_response_shows_only_assistant_text_and_tool_names(table_state) -> None:
    state, events = table_state
    display = PublicTableDisplay(report=lambda text: None)
    display.observe(state, events)
    display.activity({
        "kind": "response",
        "player_id": "alice",
        "body": '{"choices":[{"message":{"content":"Choose a low-risk card.","reasoning_content":"The full row is dangerous.","tool_calls":[{"function":{"name":"select_card","arguments":"PRIVATE RAW ARGUMENTS"}}]}}]}',
    })
    output = _render(display)
    assert "Choose a low-risk card." in output
    assert "The full row is dangerous." in output
    assert "select_card" in output
    assert "PRIVATE RAW ARGUMENTS" not in output


def test_unknown_activity_and_raw_proposals_are_not_reported() -> None:
    messages: list[str] = []
    display = PublicTableDisplay(report=messages.append)
    display.activity({"type": "proposal", "text": "FULL PRIVATE PROPOSAL"})
    display.activity({"kind": "request", "payload": {"text": "FULL PRIVATE REQUEST"}})
    display.activity({"kind": "response", "body": "bad json"})
    assert messages == []


def test_failure_status_remains_visible_with_commentary_disabled(table_state) -> None:
    state, events = table_state
    display = PublicTableDisplay(report=lambda text: None, commentary=False)
    display.observe(state, events)
    display.activity({
        "type": "invocation_failed",
        "player_id": "alice",
        "error": {"message": "Process exited 1", "private_raw": "HIDDEN"},
    })
    output = _render(display)
    assert "Operator status" in output
    assert "Process exited 1" in output
    assert "HIDDEN" not in output
    assert "Operator commentary" not in output


def test_managed_invocation_deadline_is_displayed_without_resetting_elapsed_time(table_state) -> None:
    state, events = table_state
    display = PublicTableDisplay(report=lambda text: None)
    display.observe(state, events)
    display.activity({"type": "decision_started", "player_id": "alice"})
    original_start = display._activities["alice"].started
    deadline = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()
    display.activity({"type": "invocation_started", "player_id": "alice", "deadline": deadline})
    assert display._activities["alice"].started == original_start
    assert "s left" in _render(display)


def test_terminal_startup_failure_is_reported_without_starting_animation(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("TTY_COMPATIBLE", "1")
    monkeypatch.setenv("TERM", "xterm")
    messages: list[str] = []
    with table_display(report=messages.append) as display:
        display.activity({"type": "match_finished", "outcome": "bot_failed", "reason": "Startup failed"})
    assert "Startup failed" in "\n".join(messages)
    assert capsys.readouterr().out == ""


def test_animation_can_be_disabled_on_an_interactive_terminal(
    table_state, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("TTY_COMPATIBLE", "1")
    monkeypatch.setenv("TERM", "xterm")
    state, events = table_state
    messages: list[str] = []
    with table_display(enabled=False, report=messages.append) as display:
        display.observe(state, events)
    assert "Rows " in "\n".join(messages)
    assert capsys.readouterr().out == ""


def test_completed_commentary_is_written_to_plain_scrollback_once(table_state, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTY_COMPATIBLE", "0")
    state, events = table_state
    messages: list[str] = []
    with table_display(report=messages.append) as display:
        display.observe(state, events)
        display.activity({"type": "decision_started", "player_id": "alice", "decision_number": 1})
        display.activity({
            "type": "model_text",
            "player_id": "alice",
            "text": "COMPLETE COMMENTARY ENTRY",
            "complete": True,
        })
        display.activity({
            "type": "decision_finished",
            "player_id": "alice",
            "decision_number": 1,
            "status": "accepted",
            "actions": [{"type": "select_card", "card": 20}],
        })
        display.activity({"type": "match_finished", "outcome": "completed"})
    output = "\n".join(messages)
    assert output.count("COMPLETE COMMENTARY ENTRY") == 1
    assert "Operator decision · private information" in output
    assert "Accepted: select card 20" in output


def test_commentary_uses_remaining_terminal_height_without_hiding_board(table_state) -> None:
    state, events = table_state
    display = PublicTableDisplay(report=lambda text: None)
    display.observe(state, events)
    display.activity({"type": "decision_started", "player_id": "alice", "decision_number": 1})
    display.activity({
        "type": "model_text",
        "player_id": "alice",
        "text": "\n\n".join(["Long commentary paragraph."] * 30),
    })
    console = Console(width=100, height=24, color_system=None)
    with console.capture() as capture:
        console.print(display.render(100, 24))
    output = capture.get()
    assert len(output.splitlines()) <= 24
    assert "Four rows" in output
    assert "Operator commentary" in output
    assert "More" in output


def test_late_failure_cannot_replace_accepted_activity_or_operator_status(table_state) -> None:
    state, events = table_state
    display = PublicTableDisplay(report=lambda text: None)
    display.observe(state, events)
    display.activity({"type": "decision_started", "player_id": "alice", "view_id": "view-1", "decision_number": 1})
    display.activity({
        "type": "decision_finished",
        "player_id": "alice",
        "view_id": "view-1",
        "decision_number": 1,
        "status": "accepted",
        "actions": [{"type": "select_card", "card": 20}],
    })
    display.activity({"type": "invocation_failed", "player_id": "alice", "view_id": "view-1", "text": "LATE FAILURE"})
    output = _render(display)
    assert "Accepted: select card 20" in output
    assert "LATE FAILURE" not in output
    assert display._activities["alice"].status == "accepted"


def test_old_view_failure_does_not_change_current_activity_after_history_is_trimmed(table_state) -> None:
    state, events = table_state
    display = PublicTableDisplay(report=lambda text: None)
    display.observe(state, events)
    for number in range(1, 26):
        display.activity({
            "type": "decision_started",
            "player_id": "alice",
            "view_id": f"view-{number}",
            "decision_number": number,
        })
    display.activity({
        "type": "invocation_failed",
        "player_id": "alice",
        "view_id": "view-1",
        "decision_id": "external-old",
        "text": "OLD FAILURE",
    })
    assert display._activities["alice"].status == "deciding"
    assert "OLD FAILURE" not in _render(display)
