"""Read vendor progress events without treating transcript content as actions."""

import json
from collections.abc import Callable
from typing import Any


class VendorEvents:
    """Translate complete JSONL events into optional operator-facing activity."""

    def __init__(self, kind: str, emit: Callable[[dict[str, Any]], None]) -> None:
        self.kind = kind
        self.emit = emit
        self.pending = bytearray()
        self.item_text: dict[str, str] = {}
        self.message_id = ""
        self.block_kinds: dict[str, str] = {}
        self.completed_text: set[tuple[str, str]] = set()
        self.tool_calls: dict[str, tuple[str, str]] = {}
        self.tool_statuses: dict[str, str] = {}

    def feed(self, chunk: bytes) -> None:
        self.pending.extend(chunk)
        while b"\n" in self.pending:
            line, _, remaining = self.pending.partition(b"\n")
            self.pending = remaining
            self._line(bytes(line))

    def finish(self) -> None:
        if len(self.pending) > 0:
            self._line(bytes(self.pending))
            self.pending.clear()

    def _line(self, line: bytes) -> None:
        # This path is display-only. Final-output parsing remains strict in
        # the driver, including when a client emits malformed progress.
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            return
        if not isinstance(record, dict):
            return
        if self.kind == "codex":
            self._codex(record)
        elif self.kind == "claude":
            self._claude(record)

    def _codex(self, record: dict[str, Any]) -> None:
        event_type = record.get("type")
        item = record.get("item")
        if event_type not in ("item.started", "item.updated", "item.completed") or not isinstance(item, dict):
            return
        kind = item.get("type")
        item_id = str(item.get("id", ""))
        if kind in ("agent_message", "reasoning"):
            contents = item.get("text")
            if not isinstance(contents, str):
                contents = self.item_text.get(item_id, "")
            previous = self.item_text.get(item_id, "")
            text = contents[len(previous) :] if contents.startswith(previous) else contents
            self.item_text[item_id] = contents
            self._text(
                "reasoning_summary" if kind == "reasoning" else "model_text",
                text,
                item_id,
                complete=event_type == "item.completed",
            )
            return
        if kind in ("command_execution", "mcp_tool_call", "web_search", "file_change", "todo_list"):
            name = item.get("tool") if kind == "mcp_tool_call" else kind
            if not isinstance(name, str):
                name = str(kind)
            self._tool(name, _codex_tool_status(item, event_type), item_id)

    def _claude(self, record: dict[str, Any]) -> None:
        if record.get("type") == "stream_event":
            event = record.get("event")
            if isinstance(event, dict):
                self._claude_stream(event)
            return
        if record.get("type") == "assistant":
            self._claude_message(record)
            return
        if record.get("type") == "user":
            self._claude_tool_results(record)
            return
        if record.get("type") == "system" and record.get("subtype") == "api_retry":
            attempt = record.get("attempt")
            self._tool("provider", "retrying", f"provider_retry:{attempt}")

    def _claude_stream(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "message_start":
            message = event.get("message")
            if isinstance(message, dict):
                self.message_id = str(message.get("id", ""))
            return
        index = event.get("index")
        if type(index) is not int:
            return
        item_id = f"{self.message_id}:{index}"
        if event_type == "content_block_start":
            block = event.get("content_block")
            if isinstance(block, dict):
                self._claude_block(block, item_id, complete=False)
            return
        if event_type == "content_block_stop":
            kind = self.block_kinds.get(item_id)
            if kind is not None:
                self._text(kind, "", item_id, complete=True)
            return
        if event_type != "content_block_delta":
            return
        self._claude_delta(event, item_id)

    def _claude_delta(self, event: dict[str, Any], item_id: str) -> None:
        delta = event.get("delta")
        if not isinstance(delta, dict):
            return
        kind = delta.get("type")
        field = "thinking" if kind == "thinking_delta" else "text"
        text = delta.get(field)
        if kind in ("text_delta", "thinking_delta") and isinstance(text, str) and text != "":
            kind = "reasoning_summary" if field == "thinking" else "model_text"
            self.block_kinds[item_id] = kind
            self.item_text[item_id] = self.item_text.get(item_id, "") + text
            self._text(kind, text, item_id, complete=False)

    def _claude_message(self, record: dict[str, Any]) -> None:
        message = record.get("message")
        if not isinstance(message, dict):
            return
        message_id = str(message.get("id", self.message_id))
        blocks = message.get("content")
        if not isinstance(blocks, list):
            return
        for index, block in enumerate(blocks):
            if isinstance(block, dict):
                self._claude_block(block, f"{message_id}:{index}", complete=True)

    def _claude_block(self, block: dict[str, Any], item_id: str, *, complete: bool) -> None:
        kind = block.get("type")
        if kind in ("text", "thinking"):
            record_type = "reasoning_summary" if kind == "thinking" else "model_text"
            self.block_kinds[item_id] = record_type
            text = block.get("thinking" if kind == "thinking" else "text")
            if isinstance(text, str):
                previous = self.item_text.get(item_id, "")
                self.item_text[item_id] = text
                delta = text[len(previous) :] if text.startswith(previous) else text
                self._text(record_type, delta, item_id, complete=complete)
            return
        if kind == "tool_use" and isinstance(block.get("name"), str):
            name = block["name"]
            tool_use_id = block.get("id")
            if isinstance(tool_use_id, str):
                self.tool_calls[tool_use_id] = (item_id, name)
            self._tool(name, "started", item_id)

    def _claude_tool_results(self, record: dict[str, Any]) -> None:
        message = record.get("message")
        if not isinstance(message, dict):
            return
        blocks = message.get("content")
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str):
                continue
            item_id, name = self.tool_calls.get(tool_use_id, (tool_use_id, "tool"))
            self._tool(name, "failed" if block.get("is_error") is True else "completed", item_id)

    def _text(self, kind: str, text: str, item_id: str, *, complete: bool) -> None:
        key = (kind, item_id)
        newly_complete = complete and key not in self.completed_text
        if complete:
            self.completed_text.add(key)
        if text != "" or newly_complete:
            self.emit({"type": kind, "text": text, "item_id": item_id, "delta": True, "complete": complete})

    def _tool(self, name: str, status: str, item_id: str) -> None:
        if self.tool_statuses.get(item_id) == status:
            return
        self.tool_statuses[item_id] = status
        self.emit({
            "type": "tool_activity",
            "text": f"{name}: {status}",
            "item_id": item_id,
            "tool_name": name,
            "status": status,
        })


def _codex_tool_status(item: dict[str, Any], event_type: str) -> str:
    status = item.get("status")
    exit_code = item.get("exit_code")
    if status in ("failed", "error") or (type(exit_code) is int and exit_code != 0):
        return "failed"
    if status in ("completed", "success"):
        return "completed"
    if status in ("cancelled", "canceled", "stopped"):
        return "cancelled"
    if status in ("in_progress", "running") and event_type != "item.started":
        return "running"
    return event_type.removeprefix("item.")
