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
        self.streamed_blocks: set[tuple[str, int]] = set()

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
        item_id = item.get("id")
        if kind in ("agent_message", "reasoning"):
            contents = item.get("text")
            if not isinstance(contents, str):
                return
            previous = self.item_text.get(str(item_id), "")
            text = contents[len(previous) :] if contents.startswith(previous) else contents
            self.item_text[str(item_id)] = contents
            self._text("reasoning_summary" if kind == "reasoning" else "model_text", text, str(item_id))
            return
        if kind in ("command_execution", "mcp_tool_call", "web_search", "file_change", "todo_list"):
            name = item.get("tool") if kind == "mcp_tool_call" else kind
            if not isinstance(name, str):
                name = str(kind)
            status = event_type.removeprefix("item.")
            self.emit({"type": "tool_activity", "text": f"{name}: {status}", "item_id": item_id})

    def _claude(self, record: dict[str, Any]) -> None:
        if record.get("type") == "stream_event":
            event = record.get("event")
            if isinstance(event, dict):
                self._claude_stream(event)
            return
        if record.get("type") == "assistant":
            self._claude_message(record)
            return
        if record.get("type") == "system" and record.get("subtype") == "api_retry":
            attempt = record.get("attempt")
            self.emit({"type": "tool_activity", "text": f"Provider retry {attempt}"})

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
        block_key = (self.message_id, index)
        if event_type == "content_block_start":
            block = event.get("content_block")
            if isinstance(block, dict) and self._claude_block(block, f"{self.message_id}:{index}"):
                self.streamed_blocks.add(block_key)
            return
        if event_type != "content_block_delta":
            return
        delta = event.get("delta")
        if not isinstance(delta, dict):
            return
        kind = delta.get("type")
        field = "thinking" if kind == "thinking_delta" else "text"
        text = delta.get(field)
        if kind in ("text_delta", "thinking_delta") and isinstance(text, str) and text != "":
            self.streamed_blocks.add(block_key)
            self._text("reasoning_summary" if field == "thinking" else "model_text", text, f"{self.message_id}:{index}")

    def _claude_message(self, record: dict[str, Any]) -> None:
        message = record.get("message")
        if not isinstance(message, dict):
            return
        message_id = str(message.get("id", self.message_id))
        blocks = message.get("content")
        if not isinstance(blocks, list):
            return
        for index, block in enumerate(blocks):
            if (message_id, index) not in self.streamed_blocks and isinstance(block, dict):
                self._claude_block(block, f"{message_id}:{index}")

    def _claude_block(self, block: dict[str, Any], item_id: str) -> bool:
        kind = block.get("type")
        if kind in ("text", "thinking"):
            text = block.get("thinking" if kind == "thinking" else "text")
            if isinstance(text, str) and text != "":
                self._text("reasoning_summary" if kind == "thinking" else "model_text", text, item_id)
                return True
        if kind == "tool_use" and isinstance(block.get("name"), str):
            self.emit({"type": "tool_activity", "text": f"{block['name']}: started", "item_id": item_id})
            return True
        return False

    def _text(self, kind: str, text: str, item_id: str) -> None:
        if text != "":
            self.emit({"type": kind, "text": text, "item_id": item_id, "delta": True})
