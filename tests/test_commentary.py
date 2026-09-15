"""Readable decision commentary from canonical, private activity records."""

import json

import pytest
from rich.console import Console

from sixnimmt.terminal.commentary import CommentaryBook, render_commentary, render_decision


def _start(book: CommentaryBook, *, player: str = "alice", number: int = 1, view: str = "view-1") -> None:
    book.observe({
        "type": "decision_started",
        "player_id": player,
        "display_name": player.title(),
        "decision_number": number,
        "view_id": view,
        "hand_number": 2,
        "play_number": 3,
        "phase": "selecting",
    })


def _render(book: CommentaryBook, *, width: int = 100, height: int | None = None) -> str:
    console = Console(width=width, color_system=None)
    with console.capture() as capture:
        console.print(render_commentary(book.snapshot(), width=width, height=height))
    return capture.get()


def _text(
    book: CommentaryBook,
    text: str,
    *,
    player: str = "alice",
    decision: str = "external-1",
    invocation: int = 1,
    item: str = "message-1",
    complete: bool = False,
    view: str = "view-1",
) -> None:
    book.observe({
        "type": "model_text",
        "player_id": player,
        "decision_id": decision,
        "view_id": view,
        "invocation": invocation,
        "item_id": item,
        "text": text,
        "delta": True,
        "complete": complete,
    })


def test_interleaved_messages_join_only_their_own_deltas() -> None:
    book = CommentaryBook()
    _start(book)
    _text(book, "First ", item="a")
    _text(book, "Second ", item="b")
    _text(book, "message.", item="a", complete=True)
    _text(book, "message.", item="b", complete=True)
    current = book.snapshot().current
    assert current is not None
    assert [message.text for message in current.messages] == ["First message.", "Second message."]
    assert all(message.complete for message in current.messages)


def test_new_decision_does_not_append_to_previous_message_and_late_chunks_stay_with_old_decision() -> None:
    book = CommentaryBook()
    _start(book)
    _text(book, "Old message")
    book.observe({
        "type": "decision_finished",
        "player_id": "alice",
        "decision_number": 1,
        "status": "accepted",
        "actions": [{"type": "select_card", "card": 20}],
    })
    _start(book, number=2, view="view-2")
    _text(book, "New message", decision="external-2", view="view-2")
    _text(book, " late completion", complete=True)
    output = _render(book)
    assert "New message" in output
    assert "Old message" not in output
    assert "late completion" not in output
    assert len(book.drain_completed()) == 1


def test_same_message_id_in_another_invocation_is_distinct() -> None:
    book = CommentaryBook()
    _start(book)
    _text(book, "Attempt one", invocation=1)
    _text(book, "Attempt two", invocation=2)
    current = book.snapshot().current
    assert current is not None
    assert [message.text for message in current.messages] == ["Attempt one", "Attempt two"]


def test_empty_completion_keeps_text_and_marks_message_complete() -> None:
    book = CommentaryBook()
    _start(book)
    _text(book, "Entire message")
    _text(book, "", complete=True)
    current = book.snapshot().current
    assert current is not None
    assert current.messages[0].text == "Entire message"
    assert current.messages[0].complete
    assert "streaming" not in _render(book)


def test_header_includes_predecision_context_model_and_requested_effort() -> None:
    book = CommentaryBook()
    _start(book)
    book.observe({
        "type": "invocation_started",
        "player_id": "alice",
        "decision_id": "external-1",
        "invocation": 1,
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "attempt": 2,
    })
    output = _render(book)
    assert "Hand 2 / Play 3" in output
    assert "Decision 1" in output
    assert "Attempt 2" in output
    assert "gpt-5.6-terra" in output
    assert "requested effort: high" in output


def test_markdown_paragraphs_bullets_and_code_blocks_are_rendered() -> None:
    book = CommentaryBook()
    _start(book)
    _text(
        book,
        "**Consider the rows.**\n\n- Avoid a full row.\n- Keep options open.\n\n```python\nscore = 3\n```",
        complete=True,
    )
    output = _render(book)
    assert "Consider the rows." in output
    assert "**Consider" not in output
    assert "Avoid a full row." in output
    assert "Keep options open." in output
    assert "score = 3" in output
    assert "```" not in output


def test_rich_markup_and_terminal_control_sequences_are_not_executed() -> None:
    book = CommentaryBook()
    _start(book)
    _text(book, "[red]literal[/red]\x1b[2J\u202econtrol", complete=True)
    output = _render(book)
    assert "[red]literal[/red]control" in output
    assert "\x1b" not in output
    assert "\u202e" not in output


def test_json_text_is_not_treated_as_an_accepted_action() -> None:
    book = CommentaryBook()
    _start(book)
    proposal = {
        "protocol_version": "1",
        "actions": [{"type": "select_card", "card": 99}],
        "memory": "PRIVATE NOTEBOOK",
        "session_id": "INTERNAL ID",
    }
    _text(book, json.dumps(proposal), complete=True)
    output = _render(book)
    assert "Structured output received" in output
    assert "Accepted" not in output
    assert "99" not in output
    assert "PRIVATE NOTEBOOK" not in output
    assert "INTERNAL ID" not in output


def test_validated_acceptance_replaces_provisional_choice_without_exposing_notebook() -> None:
    book = CommentaryBook()
    _start(book)
    book.observe({
        "type": "proposal",
        "player_id": "alice",
        "decision_id": "external-1",
        "proposal": {"actions": [{"type": "select_card", "card": 99}], "memory": "PRIVATE NOTEBOOK"},
    })
    assert "Proposed: select card 99" in _render(book)
    book.observe({
        "type": "decision_finished",
        "player_id": "alice",
        "decision_number": 1,
        "status": "accepted",
        "actions": [{"type": "select_card", "card": 20}],
    })
    output = _render(book)
    assert "Accepted: select card 20" in output
    assert "select card 99" not in output
    assert "PRIVATE NOTEBOOK" not in output


def test_accepted_status_without_validated_actions_does_not_promote_pending_proposal() -> None:
    book = CommentaryBook()
    _start(book)
    book.observe({
        "type": "proposal",
        "player_id": "alice",
        "proposal": {"actions": [{"type": "select_card", "card": 99}]},
    })
    book.observe({"type": "decision_finished", "player_id": "alice", "decision_number": 1, "status": "accepted"})
    assert "Accepted: select card 99" not in _render(book)


def test_explanation_is_provisional_until_decision_is_settled() -> None:
    book = CommentaryBook()
    _start(book)
    book.observe({
        "type": "decision_explanation",
        "player_id": "alice",
        "decision_id": "external-1",
        "text": "This card leaves more options.",
    })
    assert "Decision explanation · provisional" in _render(book)
    book.observe({
        "type": "decision_finished",
        "player_id": "alice",
        "decision_number": 1,
        "status": "accepted",
        "actions": [{"type": "select_card", "card": 20}],
    })
    output = _render(book)
    assert "This card leaves more options." in output
    assert "provisional" not in output


def test_tool_start_updates_and_completion_share_one_entry() -> None:
    book = CommentaryBook()
    _start(book)
    for status in ("started", "running", "completed"):
        book.observe({
            "type": "tool_activity",
            "player_id": "alice",
            "invocation": 1,
            "item_id": "tool-1",
            "tool_name": "command_execution",
            "status": status,
        })
    current = book.snapshot().current
    assert current is not None
    assert len(current.tools) == 1
    assert current.tools[0].status == "completed"
    assert "Tools: 1 completed" in _render(book)
    assert "started" not in _render(book)


def test_tool_failure_remains_visible_among_completed_tools() -> None:
    book = CommentaryBook()
    _start(book)
    book.observe({
        "type": "tool_activity",
        "player_id": "alice",
        "item_id": "failed",
        "tool_name": "lookup",
        "status": "failed",
    })
    for number in range(4):
        book.observe({
            "type": "tool_activity",
            "player_id": "alice",
            "item_id": str(number),
            "tool_name": "other",
            "status": "completed",
        })
    assert "lookup: failed" in _render(book)
    assert "4 completed" in _render(book)


@pytest.mark.parametrize(
    ("objective", "meaning", "probability"),
    [
        ({"kind": "mean"}, "Expected bull heads", False),
        ({"kind": "pickup_probability"}, "Chance of taking cards", True),
        ({"kind": "threshold_exceedance", "threshold": 5}, "Chance of exceeding 5 bull heads", True),
        ({"kind": "upper_tail", "tail_fraction": 0.1}, "Bull heads in worst 10% of outcomes", False),
    ],
)
def test_candidates_show_objective_horizon_chosen_value_and_gap(
    objective: dict, meaning: str, probability: bool
) -> None:
    book = CommentaryBook()
    _start(book)
    book.observe({
        "type": "simulation_evaluation",
        "player_id": "alice",
        "candidate_values": {"20": 0.1, "30": 0.2, "40": 0.3, "50": 0.4},
        "chosen_card": 50,
        "objective": objective,
        "horizon_plays": 2,
        "sample_count": 64,
    })
    output = _render(book)
    assert meaning in output
    assert "2 plays" in output
    assert "64 samples" in output
    assert "[50]" in output
    assert "chosen" in output
    assert "Above best" in output
    assert ("40.0%" if probability else "0.40") in output
    assert ("+30.0 pp" if probability else "+0.30") in output


def test_completed_decision_archive_includes_post_settlement_evaluation_once() -> None:
    book = CommentaryBook()
    _start(book)
    book.observe({
        "type": "decision_finished",
        "player_id": "alice",
        "decision_number": 1,
        "status": "accepted",
        "actions": [{"type": "select_card", "card": 20}],
    })
    book.observe({
        "type": "simulation_evaluation",
        "player_id": "alice",
        "decision_number": 1,
        "candidate_values": {"20": 1.5},
        "chosen_card": 20,
    })
    assert book.drain_completed() == ()
    _start(book, player="bob")
    completed = book.drain_completed()
    assert len(completed) == 1
    assert completed[0].candidates is not None
    assert completed[0].candidates.chosen == "20"
    assert book.drain_completed() == ()


def test_final_decision_is_archived_at_match_completion() -> None:
    book = CommentaryBook()
    _start(book)
    _text(book, "Useful explanation", complete=True)
    book.observe({
        "type": "decision_finished",
        "player_id": "alice",
        "decision_number": 1,
        "status": "failed",
        "text": "Timeout",
    })
    book.observe({"type": "match_finished", "outcome": "bot_failed"})
    completed = book.drain_completed()
    assert len(completed) == 1
    assert completed[0].messages[0].text == "Useful explanation"
    assert completed[0].diagnostic == "Timeout"
    assert book.drain_completed(final=True) == ()


@pytest.mark.parametrize(("width", "height"), [(40, 5), (80, 8), (100, 12)])
def test_commentary_fits_height_with_explicit_truncation(width: int, height: int) -> None:
    book = CommentaryBook()
    _start(book)
    _text(book, "\n\n".join(["An entire readable paragraph about the board."] * 30), complete=True)
    output = _render(book, width=width, height=height)
    assert len(output.splitlines()) <= height
    assert all(len(line) <= width for line in output.splitlines())
    assert "More" in output


def test_long_message_is_preserved_for_scrollback_and_bounded_at_storage_limit() -> None:
    book = CommentaryBook()
    _start(book)
    _text(book, "FIRST PARAGRAPH\n\n" + "x" * 20000, complete=True)
    book.observe({
        "type": "decision_finished",
        "player_id": "alice",
        "decision_number": 1,
        "status": "accepted",
        "actions": [],
    })
    completed = book.drain_completed(final=True)
    message = completed[0].messages[0]
    assert len(message.text) == 16000
    assert message.truncated
    assert message.text.startswith("FIRST PARAGRAPH")
    console = Console(width=80, color_system=None)
    with console.capture() as capture:
        console.print(render_decision(completed[0], history=True))
    assert "Text truncated; full output is in the model trace." in capture.get()


def test_retry_clears_old_choice_and_explanation_and_keeps_late_text_with_previous_invocation() -> None:
    book = CommentaryBook()
    _start(book)
    book.observe({"type": "invocation_started", "player_id": "alice", "invocation": 1, "decision_id": "external-1"})
    _text(book, "Old analysis", invocation=1)
    book.observe({"type": "decision_explanation", "player_id": "alice", "invocation": 1, "text": "Old explanation"})
    book.observe({
        "type": "proposal",
        "player_id": "alice",
        "invocation": 1,
        "proposal": {"actions": [{"type": "select_card", "card": 99}]},
    })
    book.observe({"type": "protocol_repair", "player_id": "alice", "invocation": 1, "text": "Try again"})
    book.observe({"type": "invocation_started", "player_id": "alice", "invocation": 2})
    _text(book, "New analysis", invocation=2)
    _text(book, " late", invocation=1, complete=True)
    output = _render(book)
    assert "New analysis" in output
    assert "Old analysis" not in output
    assert "Old explanation" not in output
    assert "select card 99" not in output
    current = book.snapshot().current
    assert current is not None
    assert current.invocation == "2"


def test_retry_request_clears_previous_attempt_before_invocation_started() -> None:
    book = CommentaryBook()
    _start(book)
    book.observe({
        "type": "decision_request",
        "player_id": "alice",
        "decision_id": "external-1",
        "invocation": 1,
        "attempt": 1,
    })
    book.observe({"type": "decision_explanation", "player_id": "alice", "invocation": 1, "text": "Old explanation"})
    book.observe({
        "type": "proposal",
        "player_id": "alice",
        "invocation": 1,
        "proposal": {"actions": [{"type": "select_card", "card": 99}]},
    })
    book.observe({"type": "protocol_repair", "player_id": "alice", "invocation": 1})
    book.observe({
        "type": "decision_request",
        "player_id": "alice",
        "decision_id": "external-1",
        "invocation": 2,
        "attempt": 2,
    })
    book.observe({"type": "invocation_started", "player_id": "alice", "invocation": 2})
    book.observe({
        "type": "decision_explanation",
        "player_id": "alice",
        "invocation": 1,
        "text": "Late old explanation",
    })
    output = _render(book)
    assert "Old explanation" not in output
    assert "Late old explanation" not in output
    assert "select card 99" not in output
    assert "Attempt 2" in output


def test_cancelled_delivery_does_not_reject_an_accepted_move() -> None:
    book = CommentaryBook()
    _start(book)
    book.observe({
        "type": "decision_finished",
        "player_id": "alice",
        "decision_number": 1,
        "status": "accepted",
        "actions": [{"type": "select_card", "card": 20}],
    })
    book.observe({"type": "delivery_cancelled", "player_id": "alice", "text": "Controller stopped"})
    output = _render(book)
    assert "Accepted: select card 20" in output
    assert "Delivery stopped: Controller stopped" in output
    assert "rejected" not in output


def test_completed_retry_history_labels_each_invocation() -> None:
    book = CommentaryBook()
    _start(book)
    _text(book, "First analysis", invocation=1, complete=True)
    _text(book, "Second analysis", invocation=2, complete=True)
    current = book.snapshot().current
    assert current is not None
    console = Console(width=100, color_system=None)
    with console.capture() as capture:
        console.print(render_decision(current, history=True))
    output = capture.get()
    assert "Invocation 1" in output
    assert "Invocation 2" in output
    assert output.index("Invocation 1") < output.index("First analysis") < output.index("Invocation 2")


@pytest.mark.parametrize("status", ["failed", "rejected"])
def test_failed_or_rejected_status_and_explanation_are_labeled_as_unsuccessful(status: str) -> None:
    book = CommentaryBook()
    _start(book)
    book.observe({"type": "decision_explanation", "player_id": "alice", "text": "Proposed explanation"})
    book.observe({
        "type": "decision_finished",
        "player_id": "alice",
        "decision_number": 1,
        "status": status,
        "attempted_actions": [{"type": "select_card", "card": 99}],
    })
    assert f"Decision explanation · {status} proposal" in _render(book)
    console = Console(width=100, color_system="standard", force_terminal=True, no_color=False)
    segments = list(console.render(render_commentary(book.snapshot(), width=100)))
    assert any(
        segment.style is not None
        and segment.style.color is not None
        and segment.style.color.name == "red"
        and f" · {status}" in segment.text
        for segment in segments
    )


def test_unknown_late_external_id_is_matched_to_its_retained_view() -> None:
    book = CommentaryBook()
    _start(book, number=1, view="view-old")
    _start(book, number=2, view="view-current")
    book.observe({
        "type": "model_text",
        "player_id": "alice",
        "decision_id": "late-unknown",
        "view_id": "view-old",
        "text": "OLD VIEW TEXT",
    })
    current = book.snapshot().current
    assert current is not None
    assert current.number == "2"
    assert current.messages == ()
    assert "OLD VIEW TEXT" not in _render(book)


def test_unretained_stale_view_is_not_attached_to_current_decision() -> None:
    book = CommentaryBook()
    _start(book, number=2, view="view-current")
    book.observe({
        "type": "model_text",
        "player_id": "alice",
        "decision_id": "unretained-old",
        "view_id": "view-old",
        "text": "STALE VIEW TEXT",
    })
    current = book.snapshot().current
    assert current is not None
    assert current.messages == ()
    assert "STALE VIEW TEXT" not in _render(book)


def test_external_alias_storage_is_bounded_even_for_one_decision() -> None:
    book = CommentaryBook()
    _start(book)
    for number in range(1000):
        book.observe({
            "type": "decision_request",
            "player_id": "alice",
            "decision_id": f"external-{number}",
            "view_id": "view-1",
        })
    assert len(book._decisions) == 1
    assert len(book._external) <= 288


def test_accepted_outcome_and_choice_survive_late_failure_invocation_and_proposal() -> None:
    book = CommentaryBook()
    _start(book)
    book.observe({
        "type": "invocation_started",
        "player_id": "alice",
        "decision_id": "external-1",
        "view_id": "view-1",
        "invocation": 1,
    })
    book.observe({
        "type": "decision_finished",
        "player_id": "alice",
        "decision_number": 1,
        "status": "accepted",
        "actions": [{"type": "select_card", "card": 20}],
    })
    book.observe({
        "type": "invocation_failed",
        "player_id": "alice",
        "decision_id": "external-1",
        "view_id": "view-1",
        "invocation": 1,
        "text": "Late process failure",
    })
    book.observe({
        "type": "invocation_started",
        "player_id": "alice",
        "decision_id": "external-1",
        "view_id": "view-1",
        "invocation": 2,
    })
    book.observe({
        "type": "proposal",
        "player_id": "alice",
        "decision_id": "external-1",
        "view_id": "view-1",
        "invocation": 2,
        "proposal": {"actions": [{"type": "select_card", "card": 99}]},
    })
    current = book.snapshot().current
    assert current is not None
    assert current.status == "accepted"
    assert current.invocation == "1"
    assert current.choices == ("select card 20",)
    assert "Accepted: select card 20" in _render(book)


def test_late_record_for_evicted_view_cannot_change_newest_decision() -> None:
    book = CommentaryBook()
    for number in range(1, 26):
        _start(book, number=number, view=f"view-{number}")
        book.observe({
            "type": "decision_request",
            "player_id": "alice",
            "decision_id": f"external-{number}",
            "view_id": f"view-{number}",
        })
    book.observe({
        "type": "invocation_failed",
        "player_id": "alice",
        "decision_id": "external-1",
        "view_id": "view-1",
        "text": "OLD FAILURE",
    })
    current = book.snapshot().current
    assert current is not None
    assert current.number == "25"
    assert current.status == "deciding"
    assert "OLD FAILURE" not in _render(book)
