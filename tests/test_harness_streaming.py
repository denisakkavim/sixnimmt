"""Only client-exposed commentary and activity enter the live model display."""

import json
from typing import Any

import pytest

from sixnimmt.arena.bots.external_harnesses.streaming import VendorEvents


def _feed(events: VendorEvents, records: list[dict[str, Any]]) -> None:
    for record in records:
        events.feed(json.dumps(record).encode() + b"\n")


def test_codex_updated_and_completed_messages_do_not_repeat_text() -> None:
    output: list[dict[str, Any]] = []
    events = VendorEvents("codex", output.append)
    _feed(
        events,
        [
            {"type": "item.updated", "item": {"id": "a", "type": "agent_message", "text": "Choose "}},
            {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "Choose 12"}},
            {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "Choose 12"}},
        ],
    )
    assert [record["text"] for record in output] == ["Choose ", "12"]
    assert [record["complete"] for record in output] == [False, True]


def test_codex_reports_exposed_reasoning_and_tool_status_without_command_arguments() -> None:
    output: list[dict[str, Any]] = []
    events = VendorEvents("codex", output.append)
    _feed(
        events,
        [
            {"type": "item.completed", "item": {"id": "a", "type": "reasoning", "text": "Compare row risks"}},
            {
                "type": "item.started",
                "item": {"id": "b", "type": "command_execution", "command": "tool --token do-not-display"},
            },
        ],
    )
    assert [(record["type"], record["text"]) for record in output] == [
        ("reasoning_summary", "Compare row risks"),
        ("tool_activity", "command_execution: started"),
    ]
    assert "do-not-display" not in json.dumps(output)
    assert output[-1]["tool_name"] == "command_execution"
    assert output[-1]["status"] == "started"
    assert output[-1]["item_id"] == "b"


def test_claude_streamed_text_and_thinking_are_not_repeated_by_completed_message() -> None:
    output: list[dict[str, Any]] = []
    events = VendorEvents("claude", output.append)
    _feed(
        events,
        [
            {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}},
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Choose 12"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "thinking_delta", "thinking": "The first row is cheap"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "signature_delta", "signature": "opaque-signature"},
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "m1",
                    "content": [
                        {"type": "text", "text": "Choose 12"},
                        {"type": "thinking", "thinking": "The first row is cheap", "signature": "opaque-signature"},
                    ],
                },
            },
        ],
    )
    assert [(record["type"], record["text"]) for record in output] == [
        ("model_text", "Choose 12"),
        ("reasoning_summary", "The first row is cheap"),
        ("model_text", ""),
        ("reasoning_summary", ""),
    ]
    assert [(record["item_id"], record["complete"]) for record in output] == [
        ("m1:0", False),
        ("m1:1", False),
        ("m1:0", True),
        ("m1:1", True),
    ]
    assert "opaque-signature" not in json.dumps(output)


def test_claude_complete_messages_work_when_partial_events_are_unavailable() -> None:
    output: list[dict[str, Any]] = []
    events = VendorEvents("claude", output.append)
    _feed(
        events,
        [
            {
                "type": "assistant",
                "message": {
                    "id": "m1",
                    "content": [
                        {"type": "text", "text": "Checking the board"},
                        {"type": "tool_use", "name": "Read", "input": {"path": "private-file"}},
                        {"type": "redacted_thinking", "data": "opaque-data"},
                    ],
                },
            }
        ],
    )
    assert [(record["type"], record["text"]) for record in output] == [
        ("model_text", "Checking the board"),
        ("tool_activity", "Read: started"),
    ]
    assert output[0]["complete"] is True


def test_split_utf8_json_line_is_displayed_after_completion_without_requiring_newline() -> None:
    output: list[dict[str, Any]] = []
    events = VendorEvents("codex", output.append)
    line = json.dumps(
        {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "6 nimmt! 🐂"}},
        ensure_ascii=False,
    ).encode()
    split = line.index("🐂".encode()) + 1
    events.feed(line[:split])
    assert output == []
    events.feed(line[split:])
    events.finish()
    assert output[0]["text"] == "6 nimmt! 🐂"


def test_malformed_and_unknown_progress_records_do_not_become_model_text() -> None:
    output: list[dict[str, Any]] = []
    events = VendorEvents("claude", output.append)
    events.feed(b'not-json\n{"type":"future_event","text":"ignored"}\n[]\n')
    events.finish()
    assert output == []


@pytest.mark.parametrize(
    "item_type, record_type", [("agent_message", "model_text"), ("reasoning", "reasoning_summary")]
)
def test_codex_same_text_completion_emits_one_explicit_boundary(item_type: str, record_type: str) -> None:
    output: list[dict[str, Any]] = []
    events = VendorEvents("codex", output.append)
    _feed(
        events,
        [
            {"type": "item.updated", "item": {"id": "a", "type": item_type, "text": "Already streamed"}},
            {"type": "item.completed", "item": {"id": "a", "type": item_type, "text": "Already streamed"}},
            {"type": "item.completed", "item": {"id": "a", "type": item_type, "text": "Already streamed"}},
        ],
    )
    assert output == [
        {"type": record_type, "text": "Already streamed", "item_id": "a", "delta": True, "complete": False},
        {"type": record_type, "text": "", "item_id": "a", "delta": True, "complete": True},
    ]


@pytest.mark.parametrize(
    "event_type, vendor_status, exit_code, expected",
    [
        ("item.started", "in_progress", None, "started"),
        ("item.updated", "in_progress", None, "running"),
        ("item.completed", "completed", 0, "completed"),
        ("item.completed", "failed", None, "failed"),
        ("item.completed", "completed", 1, "failed"),
        ("item.completed", "cancelled", None, "cancelled"),
    ],
)
def test_codex_tool_status_comes_from_vendor_outcome_fields(
    event_type: str, vendor_status: str, exit_code: int | None, expected: str
) -> None:
    output: list[dict[str, Any]] = []
    events = VendorEvents("codex", output.append)
    _feed(
        events,
        [
            {
                "type": event_type,
                "item": {
                    "id": "call_1",
                    "type": "mcp_tool_call",
                    "tool": "inspect_board",
                    "status": vendor_status,
                    "exit_code": exit_code,
                    "arguments": {"private": "PRIVATE ARGUMENT"},
                    "result": "PRIVATE RESULT",
                },
            }
        ],
    )
    assert output == [
        {
            "type": "tool_activity",
            "text": f"inspect_board: {expected}",
            "item_id": "call_1",
            "tool_name": "inspect_board",
            "status": expected,
        }
    ]


@pytest.mark.parametrize(
    "block_type, field, record_type", [("text", "text", "model_text"), ("thinking", "thinking", "reasoning_summary")]
)
def test_claude_block_stop_completes_text_without_duplicate_assistant_boundary(
    block_type: str, field: str, record_type: str
) -> None:
    output: list[dict[str, Any]] = []
    events = VendorEvents("claude", output.append)
    _feed(
        events,
        [
            {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}},
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": block_type, field: "Some text"},
                },
            },
            {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
            {"type": "assistant", "message": {"id": "m1", "content": [{"type": block_type, field: "Some text"}]}},
        ],
    )
    assert output == [
        {"type": record_type, "text": "Some text", "item_id": "m1:0", "delta": True, "complete": False},
        {"type": record_type, "text": "", "item_id": "m1:0", "delta": True, "complete": True},
    ]


@pytest.mark.parametrize("is_error, status", [(False, "completed"), (True, "failed")])
def test_claude_tool_finishes_on_tool_result_instead_of_input_block_stop(is_error: bool, status: str) -> None:
    output: list[dict[str, Any]] = []
    events = VendorEvents("claude", output.append)
    tool = {"type": "tool_use", "id": "tool_1", "name": "Read", "input": {"file": "PRIVATE INPUT"}}
    _feed(
        events,
        [
            {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}},
            {"type": "stream_event", "event": {"type": "content_block_start", "index": 1, "content_block": tool}},
            {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}},
            {"type": "assistant", "message": {"id": "m1", "content": [{"type": "redacted_thinking"}, tool]}},
        ],
    )
    assert len(output) == 1
    assert output[0]["status"] == "started"
    _feed(
        events,
        [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_1",
                            "is_error": is_error,
                            "content": "PRIVATE RESULT",
                        }
                    ]
                },
            }
        ],
    )
    assert output[-1] == {
        "type": "tool_activity",
        "text": f"Read: {status}",
        "item_id": "m1:1",
        "tool_name": "Read",
        "status": status,
    }
    assert "PRIVATE" not in json.dumps(output)


def test_json_in_completed_assistant_text_remains_commentary() -> None:
    output: list[dict[str, Any]] = []
    events = VendorEvents("codex", output.append)
    text = '{"actions":[{"type":"select_card","card":12}]}'
    _feed(events, [{"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": text}}])
    assert output[0]["type"] == "model_text"
    assert output[0]["text"] == text
    assert output[0]["complete"] is True
