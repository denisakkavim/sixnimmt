"""Recovery distinguishes interrupted writes from complete invalid records."""

import json
from functools import partial
from pathlib import Path

import pytest
from pydantic import ValidationError

from sixnimmt.engine.events import MatchStartedEvent
from sixnimmt.persistence import sink
from sixnimmt.persistence.atomic import write_json
from sixnimmt.persistence.manifest import ManifestMatch, read_manifest_entry
from sixnimmt.persistence.sink import (
    AtomicJsonlWriter,
    JsonlEventSink,
    LogRecordError,
    pending_path,
    read_event_log,
    read_jsonl,
)


def valid_record() -> str:
    return MatchStartedEvent(match_id="example", audience="public").model_dump_json()


@pytest.mark.parametrize("tail", ['{"type":', '{"type":"unfinished', '{"type":"match_started"'])
def test_recovers_incomplete_final_json_after_valid_record(tmp_path, tail: str) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(valid_record() + "\n" + tail)
    assert len(read_event_log(path)) == 1


@pytest.mark.parametrize("tail", ['{"type":"unknown"}\n', '{"type":"unknown"}', "{bad", '{"type":\n'])
def test_rejects_complete_or_ambiguous_invalid_tail_with_location(tmp_path, tail: str) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(valid_record() + "\n\n" + tail)
    with pytest.raises(LogRecordError, match=r"events.jsonl:3:"):
        read_event_log(path)


def test_reads_valid_final_record_without_newline(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(valid_record())
    assert len(read_event_log(path)) == 1


def test_rejects_incomplete_only_record(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"type":')
    with pytest.raises(LogRecordError, match=r"events.jsonl:1:"):
        read_event_log(path)


def test_rejects_invalid_middle_record(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(valid_record() + '\n{"type":\n' + valid_record())
    with pytest.raises(LogRecordError, match=r"events.jsonl:2:"):
        read_event_log(path)


def test_pending_sidecar_excludes_uncommitted_tail_before_parsing(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    committed = valid_record() + "\n"
    path.write_text(committed + '{"type":"unknown"}\n')
    pending_path(path).write_text(json.dumps({"committed_length": len(committed.encode())}))
    assert len(read_event_log(path)) == 1


def test_generic_reader_recovers_only_committed_complete_values(tmp_path) -> None:
    path = tmp_path / "values.jsonl"
    path.write_text('1\n2\n{"unfinished":')
    assert read_jsonl(path, int) == [1, 2]


def test_atomic_json_rejects_invalid_payload_without_replacing_existing_file(tmp_path) -> None:
    path = tmp_path / "manifest.json"
    write_json(path, {"version": 1})
    with pytest.raises(ValueError):
        write_json(path, {"invalid": float("nan")})
    assert json.loads(path.read_text()) == {"version": 1}
    assert not path.with_suffix(".json.tmp").exists()


def _track_writer(opened: list[AtomicJsonlWriter], path: Path) -> AtomicJsonlWriter:
    writer = AtomicJsonlWriter(path)
    opened.append(writer)
    return writer


@pytest.mark.parametrize("failure", ["action_open", "event_read"])
def test_sink_constructor_closes_opened_files_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    opened: list[AtomicJsonlWriter] = []
    monkeypatch.setattr(sink, "AtomicJsonlWriter", partial(_track_writer, opened))
    if failure == "action_open":
        (tmp_path / "game.actions.jsonl").mkdir()
    else:
        (tmp_path / "game.jsonl").write_text("{invalid\n")
    with pytest.raises((OSError, LogRecordError)):
        JsonlEventSink(tmp_path, "game")
    assert len(opened) == (1 if failure == "action_open" else 2)
    assert all(writer._handle.closed for writer in opened)


@pytest.fixture
def trace_entry() -> ManifestMatch:
    return ManifestMatch(
        game_index=0,
        match_id="example",
        seed=1,
        outcome="finished",
        log="example.jsonl",
        actions="example.actions.jsonl",
    )


@pytest.mark.parametrize("location", ["manifest", "entry"])
@pytest.mark.parametrize("version", [True, False, 1.0, "1", 2])
def test_trace_reader_rejects_invalid_schema_versions(
    tmp_path: Path, trace_entry: ManifestMatch, location: str, version: object
) -> None:
    entry = trace_entry.model_dump(mode="json")
    manifest: dict[str, object] = {"manifest_version": 1, "matches": [entry]}
    if location == "manifest":
        manifest["manifest_version"] = version
    else:
        entry["manifest_version"] = version
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValidationError):
        read_manifest_entry(path, trace_entry.log)


@pytest.mark.parametrize("include_entry_version", [True, False])
def test_trace_reader_preserves_version_one_and_legacy_entry_default(
    tmp_path: Path, trace_entry: ManifestMatch, include_entry_version: bool
) -> None:
    entry = trace_entry.model_dump(mode="json")
    if not include_entry_version:
        del entry["manifest_version"]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"manifest_version": 1, "matches": [entry]}))
    restored = read_manifest_entry(path, trace_entry.log)
    assert restored == trace_entry
    assert type(restored.manifest_version) is int
    assert restored.manifest_version == 1
