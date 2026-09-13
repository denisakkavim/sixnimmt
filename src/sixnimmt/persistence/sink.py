"""Durable event and action logs shared by match surfaces."""

import json
import os
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from sixnimmt.engine.events import Event

_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


class ActionRecord(BaseModel):
    """One submitted action, in the order the arena processed it.

    `server_action_seq` is the canonical order; `received_at` is for latency
    analysis only, and nothing may replay a match from timestamps.
    """

    model_config = ConfigDict(frozen=True)

    server_action_seq: int
    action_id: str
    player_id: str
    type: str | None
    # The folded observation offered for this decision, so the audit record can
    # be read back against what the bot could see.
    from_view: str | None
    received_at: datetime
    outcome: Literal["accepted", "rejected", "timeout", "error"] = "accepted"
    reason: str | None = None
    decision_started_at: datetime | None = None
    decision_ended_at: datetime | None = None
    decision_duration_ms: float | None = None


class EventSink(Protocol):
    """Where a match's events and action records go to outlive the process."""

    def append(self, events: Sequence[Event]) -> None: ...

    def record_action(self, record: ActionRecord) -> None: ...

    def close(self) -> None: ...


class NullEventSink(EventSink):
    """Persists nothing. What an in-process match gets when no log is wanted."""

    def append(self, events: Sequence[Event]) -> None:
        return

    def record_action(self, record: ActionRecord) -> None:
        return

    def close(self) -> None:
        return


class AtomicJsonlWriter:
    """An append-only JSONL file whose batches land whole or not at all.

    A batch is staged in a sidecar alongside the log, carrying the log's length
    before the batch, and forced to disk before the log itself is touched. A
    crash in between leaves the sidecar behind, and the next open truncates the
    log back to that recorded length. An unacknowledged batch is therefore
    discarded entire, rather than replayed as a fraction of a transition that
    the live match never committed.

    Readers apply the same rule without writing anything: `committed_text`
    stops at the staged length, so a log recovered by a later process and one
    merely read in place fold to the same events.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._pending = pending_path(path)
        self._recover()
        # Reopening a match preserves its committed history, including after
        # abandonment; only an unacknowledged sidecar batch is rolled back.
        self._handle = path.open("a", encoding="utf-8")

    def _recover(self) -> None:
        """Undo a batch that was staged but never acknowledged."""
        staged = _staged_length(self._path)
        if staged is not None and self._path.exists():
            with self._path.open("r+", encoding="utf-8") as handle:
                handle.truncate(staged)
        self._pending.unlink(missing_ok=True)
        _sync_directory(self._pending)

    def write(self, payloads: list[dict[str, Any]]) -> None:
        lines = "".join(json.dumps(payload, sort_keys=True) + "\n" for payload in payloads)
        self._stage(lines)
        # One call for the whole batch, then forced to the platter before the
        # caller is told it was written: flushing alone leaves the tail of a
        # match to the operating system's discretion.
        self._handle.write(lines)
        self._handle.flush()
        os.fsync(self._handle.fileno())
        # Only now is the batch committed, and the intent to write it spent.
        # Its removal has to outlive a crash too, or recovery would undo a
        # batch this call is about to report as durable.
        self._pending.unlink(missing_ok=True)
        _sync_directory(self._pending)

    def _stage(self, lines: str) -> None:
        """Record where the log ends and what is about to be added to it."""
        with self._pending.open("w", encoding="utf-8") as handle:
            # The file's size on disk, not the handle's position: a text handle
            # in append mode reports an offset that is not a byte count.
            committed_length = self._path.stat().st_size
            json.dump({"committed_length": committed_length, "bytes": len(lines)}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        _sync_directory(self._pending)

    def close(self) -> None:
        self._handle.close()


def pending_path(path: Path) -> Path:
    return path.with_name(path.name + ".pending")


def _sync_directory(path: Path) -> None:
    """Make the creation or removal of a file's directory entry durable.

    Unlinking a file is not itself on disk until its parent directory is
    synced. Without this a crash could resurrect a sidecar that had already
    been spent, and recovery would then roll back a batch that the match had
    committed and published. Platforms that cannot sync a directory simply do
    not gain the guarantee; nothing else depends on it.
    """
    try:
        handle = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _staged_length(path: Path) -> int | None:
    """How long the log was before an unacknowledged batch, if there was one.

    A sidecar too damaged to read is one that a crash caught mid-stage, before
    the log itself was touched, so there is nothing to undo and no length to
    report.
    """
    pending = pending_path(path)
    if not pending.exists():
        return None
    try:
        staged = json.loads(pending.read_text(encoding="utf-8"))
        return int(staged["committed_length"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def committed_text(path: Path) -> str:
    """The log as far as its last acknowledged batch."""
    text = path.read_text(encoding="utf-8")
    staged = _staged_length(path)
    if staged is None:
        return text
    # Byte length on disk, character offset in memory: the log is written as
    # ASCII-safe JSON, so encoding once is cheaper than reasoning about both.
    return text.encode("utf-8")[:staged].decode("utf-8")


class JsonlEventSink(EventSink):
    """One JSONL file per match, with its action records beside it.

    Nothing is filtered on the way in. The log is the durable record of the
    whole match, admin events and seeds included; audience filtering belongs to
    the moment a viewer reads, never to the moment the arena writes.
    """

    def __init__(self, directory: Path, match_id: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self._events = AtomicJsonlWriter(event_log_path(directory, match_id))
        self._actions = AtomicJsonlWriter(action_log_path(directory, match_id))
        self._closed = False
        self._model_path = directory / f"{match_id}.model.jsonl"
        self._model_lock = Lock()
        self._player_names: dict[str, str] = {}
        # A reopened sink must retain the labels learned when the match began.
        for event in read_event_log(event_log_path(directory, match_id)):
            self._remember_names(event)

    def append(self, events: Sequence[Event]) -> None:
        if len(events) == 0:
            return
        payloads = []
        for event in events:
            self._remember_names(event)
            payloads.append(self._with_player_names(event.model_dump(mode="json")))
        self._write(self._events, payloads)

    def record_action(self, record: ActionRecord) -> None:
        self._write(self._actions, [self._with_player_names(record.model_dump(mode="json"))])

    def record_model(self, payload: dict[str, Any], *, player_id: str, display_name: str) -> None:
        """Write privileged diagnostics, including responses arriving after match timeout.

        Each append owns its handle so late model responses do not use closed
        game-log handles. The lock serialises requests from different seats.
        """
        with self._model_lock:
            writer = AtomicJsonlWriter(self._model_path)
            try:
                writer.write([{**payload, "player_id": player_id, "display_name": display_name}])
            finally:
                writer.close()

    def _remember_names(self, event: Event) -> None:
        if event.type != "match_created" or event.audience != "public":
            return
        self._player_names = {player["player_id"]: player["display_name"] for player in event.data["players"]}

    def _with_player_names(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Label explicit identity references without interpreting message text.

        This top-level annotation is ignored by event/action readers. Keeping it
        outside event data preserves the authoritative event and replay contract.
        """
        data = payload.get("data", payload)
        player_ids = set()
        for key in ("player_id", "from", "to", "ended_by"):
            player_id = data.get(key)
            if isinstance(player_id, str):
                player_ids.add(player_id)
        for key in ("selections", "hand_scores", "totals"):
            player_ids.update(data.get(key, {}))
        player_ids.update(data.get("winners", []))
        for player in data.get("players", []):
            player_ids.add(player["player_id"])
        audience = payload.get("audience", "")
        if audience.startswith("player:"):
            player_ids.add(audience.removeprefix("player:"))
        names = {
            player_id: self._player_names[player_id]
            for player_id in sorted(player_ids)
            if player_id in self._player_names
        }
        if len(names) > 0:
            payload["player_display_names"] = names
        return payload

    def _write(self, writer: AtomicJsonlWriter, payloads: list[dict[str, Any]]) -> None:
        if self._closed:
            # Abandonment releases the match's handles while its log stays on
            # disk. Nothing can be applied to the match afterwards, so a late
            # write has nothing left to record.
            return
        writer.write(payloads)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._events.close()
        self._actions.close()


def event_log_path(directory: Path, match_id: str) -> Path:
    return directory / f"{match_id}.jsonl"


def action_log_path(directory: Path, match_id: str) -> Path:
    return directory / f"{match_id}.actions.jsonl"


class LogRecordError(ValueError):
    """A corrupt record, identified by file and physical line."""


def _incomplete_json(error: json.JSONDecodeError, line: str) -> bool:
    # Only recover EOF inside a token/container. Other syntax errors are corruption,
    # even on the last line. A newline is evidence the writer completed the record.
    if line.endswith(("\n", "\r")):
        return False
    return error.msg.startswith("Unterminated string") or error.pos >= len(line.rstrip())


def _parse_log(path: Path, parse: Callable[[str], Any]) -> list[Any]:
    """Read committed records; recover only an incomplete final JSON write.

    Schema errors always fail. An entirely unreadable file also fails, preserving
    the distinction between an empty log and a file that is not a log at all.
    """
    lines = committed_text(path).splitlines(keepends=True)
    records: list[Any] = []
    for index, line in enumerate(lines):
        if line.strip() == "":
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError as error:
            if index == len(lines) - 1 and len(records) > 0 and _incomplete_json(error, line):
                break
            msg = f"{path}:{index + 1}: invalid JSON: {error}"
            raise LogRecordError(msg) from error
        try:
            records.append(parse(line))
        except ValidationError as error:
            msg = f"{path}:{index + 1}: invalid record: {error}"
            raise LogRecordError(msg) from error
    return records


def read_event_log(path: Path) -> list[Event]:
    """Every event in a match log, in the order the arena wrote it."""
    return _parse_log(path, _EVENT_ADAPTER.validate_json)


def read_action_log(path: Path) -> list[ActionRecord]:
    """Every action record, already in `server_action_seq` order.

    Records are written inside the per-match serialisation boundary, so the file
    order is the canonical processing order, independent of timestamps.
    """
    return _parse_log(path, ActionRecord.model_validate_json)
