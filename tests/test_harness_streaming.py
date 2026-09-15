"""Only client-exposed commentary and activity enter the live model display."""

import json
from typing import Any

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
