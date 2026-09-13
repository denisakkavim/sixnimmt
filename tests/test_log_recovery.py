"""Recovery distinguishes interrupted writes from complete invalid records."""

import json

import pytest

from sixnimmt.engine.events import MatchStartedEvent
from sixnimmt.persistence.sink import LogRecordError, pending_path, read_event_log


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
