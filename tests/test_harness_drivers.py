"""Managed harnesses consume one request and publish only completed final JSON."""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import Any, Literal

import pytest

from sixnimmt.arena.bots.external_harnesses import drivers as harness_drivers
from sixnimmt.arena.bots.external_harnesses.drivers import (
    CommandProfile,
    DriverDeadlineExceeded,
    DriverError,
    ManagedCommandDriver,
    parse_final_json,
    structured_proposal_schema,
)
from sixnimmt.arena.bots.external_harnesses.protocol import PROPOSAL_SCHEMA


def make_driver(
    tmp_path: Path,
    source: str,
    *,
    kind: Literal["command", "codex", "claude"] = "command",
    timeout_seconds: float = 120,
    max_output_bytes: int = 1_048_576,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> ManagedCommandDriver:
    script = tmp_path / "agent.py"
    script.write_text(source, encoding="utf-8")
    profile = CommandProfile(
        (sys.executable, str(script)), kind, timeout_seconds, max_output_bytes, model, reasoning_effort
    )
    return ManagedCommandDriver(profile, tmp_path / "workspace")


@pytest.mark.parametrize("model", ["", " ", "\t\n"])
def test_command_profile_rejects_empty_model(model: str) -> None:
    with pytest.raises(ValueError, match="model must be a nonempty string"):
        CommandProfile(("codex",), kind="codex", model=model)


def test_generic_command_profile_rejects_vendor_model_setting() -> None:
    with pytest.raises(ValueError, match="model is only supported for Codex and Claude"):
        CommandProfile(("custom-agent",), model="chosen-model")


@pytest.mark.parametrize("effort", ["", " ", "\t\n"])
@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_vendor_profile_rejects_empty_reasoning_effort(kind: Literal["codex", "claude"], effort: str) -> None:
    with pytest.raises(ValueError, match="reasoning_effort must be a nonempty string"):
        CommandProfile((kind,), kind=kind, reasoning_effort=effort)


def test_generic_command_profile_rejects_vendor_reasoning_setting() -> None:
    with pytest.raises(ValueError, match="reasoning_effort is only supported for Codex and Claude"):
        CommandProfile(("custom-agent",), reasoning_effort="high")


@pytest.mark.parametrize("effort", ["none", "minimal", "ultra", "ultracode", "HIGH"])
def test_claude_profile_rejects_values_outside_its_model_effort_levels(effort: str) -> None:
    with pytest.raises(ValueError, match="Claude reasoning_effort must be"):
        CommandProfile(("claude",), kind="claude", reasoning_effort=effort)


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_claude_profile_accepts_its_named_effort_levels_without_model_mapping(effort: str) -> None:
    profile = CommandProfile(("claude",), kind="claude", model="future-model", reasoning_effort=effort)
    assert profile.reasoning_effort == effort


def test_codex_effort_values_remain_open_to_new_model_advertised_names() -> None:
    profile = CommandProfile(("codex",), kind="codex", model="future-model", reasoning_effort="future-effort")
    assert profile.reasoning_effort == "future-effort"


def test_generic_driver_receives_filtered_offer_and_returns_final_object(tmp_path: Path) -> None:
    driver = make_driver(
        tmp_path,
        "import json, sys\nrequest = json.load(sys.stdin)\nprint(json.dumps(request['offer']))\n",
    )
    result = driver.invoke({"decision_id": "decision-1"}, {"rules": "game instructions"}, {})
    assert result == {"decision_id": "decision-1"}
    assert list((tmp_path / "workspace").iterdir()) == []


def test_every_driver_record_carries_current_decision_view_and_generation_settings(tmp_path: Path) -> None:
    message = {
        "type": "item.completed",
        "item": {"id": "message_1", "type": "agent_message", "text": "A concise explanation"},
    }
    source = (
        "import pathlib, sys\n"
        "sys.stdin.read()\n"
        f"print({json.dumps(message)!r}, flush=True)\n"
        "print('Client diagnostic', file=sys.stderr, flush=True)\n"
        "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('{}')\n"
    )
    driver = make_driver(tmp_path, source, kind="codex", model="chosen-model", reasoning_effort="high")
    records: list[dict[str, Any]] = []
    driver.set_trace(records.append)
    for invocation in (1, 2):
        records.clear()
        offer = {"decision_id": f"decision-{invocation}", "view_id": f"view-{invocation}"}
        assert driver.invoke(offer, {}, {}) == {}
        driver.emit_trace({"type": "proposal", "proposal": {"actions": []}})
        assert {record["type"] for record in records} == {
            "invocation_started",
            "model_text",
            "stderr",
            "invocation_output",
            "invocation_completed",
            "proposal",
        }
        for record in records:
            assert record["decision_id"] == offer["decision_id"]
            assert record["view_id"] == offer["view_id"]
            assert record["invocation"] == invocation
            assert record["client"] == "codex"
            assert record["model"] == "chosen-model"
            assert record["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    "contents",
    [b'{"a":1}\n{"a":2}', b'[{"a":1}]', b'{"a":1,"a":2}', b'{"a":NaN}', b"```json\n{}\n```"],
)
def test_final_json_rejects_transcripts_duplicates_and_invalid_numbers(contents: bytes) -> None:
    with pytest.raises(DriverError):
        parse_final_json(contents)


def test_nonzero_exit_discards_even_valid_json(tmp_path: Path) -> None:
    driver = make_driver(tmp_path, "import sys\nprint('{}')\nsys.exit(3)\n")
    with pytest.raises(DriverError, match="status 3"):
        driver.invoke({}, {}, {})


def test_output_limit_terminates_unbounded_output(tmp_path: Path) -> None:
    driver = make_driver(tmp_path, "while True:\n print('x' * 8192, flush=True)\n", max_output_bytes=16384)
    with pytest.raises(DriverError, match="managed_output_limit"):
        driver.invoke({}, {}, {})


def test_managed_timeout_applies_without_an_arena_deadline(tmp_path: Path) -> None:
    driver = make_driver(tmp_path, "import time\ntime.sleep(10)\n", timeout_seconds=0.1)
    before = time.monotonic()
    with pytest.raises(DriverDeadlineExceeded, match="managed_decision_timeout"):
        driver.invoke({}, {}, {})
    assert time.monotonic() - before < 3


@pytest.mark.parametrize("remaining, expected", [(None, 30), (3, 3), (60, 30)])
def test_invocation_trace_deadline_uses_the_shorter_managed_or_arena_budget(
    tmp_path: Path, remaining: int | None, expected: int
) -> None:
    driver = make_driver(tmp_path, "print('{}')\n", timeout_seconds=30)
    records: list[dict[str, Any]] = []
    driver.set_trace(records.append)
    before = datetime.now(UTC)
    deadline = time.monotonic() + remaining if remaining is not None else None
    assert driver.invoke({}, {}, {}, deadline=deadline) == {}
    after = datetime.now(UTC)
    started = records[0]
    assert started["type"] == "invocation_started"
    traced_deadline = datetime.fromisoformat(started["deadline"])
    assert before + timedelta(seconds=expected - 0.05) <= traced_deadline
    assert traced_deadline <= after + timedelta(seconds=expected + 0.05)


def _invoke_and_capture(driver: ManagedCommandDriver, errors: list[Exception]) -> None:
    try:
        driver.invoke({}, {}, {})
    except Exception as error:
        errors.append(error)


def test_cancellation_reaps_a_running_invocation(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    source = f"import pathlib, time\npathlib.Path({str(marker)!r}).write_text('started')\ntime.sleep(10)\n"
    driver = make_driver(tmp_path, source)
    errors: list[Exception] = []
    thread = Thread(target=_invoke_and_capture, args=(driver, errors))
    thread.start()
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    driver.cancel()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert str(errors[0]) == "managed_invocation_cancelled"


def test_codex_reads_designated_final_file_and_ignores_intermediate_stdout(tmp_path: Path) -> None:
    driver = make_driver(
        tmp_path,
        "import pathlib, sys\n"
        "result = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        'result.write_text(\'{"submission_id":"final"}\')\n'
        'print(\'{"submission_id":"intermediate"}\')\n',
        kind="codex",
    )
    assert driver.invoke({}, {}, {}) == {"submission_id": "final"}


def test_claude_reads_only_structured_output_of_successful_result(tmp_path: Path) -> None:
    response = {"type": "result", "subtype": "success", "structured_output": {"submission_id": "final"}}
    driver = make_driver(tmp_path, f"print({json.dumps(response)!r})\n", kind="claude")
    assert driver.invoke({}, {}, {}) == {"submission_id": "final"}


@pytest.mark.parametrize("kind", ["codex", "claude"])
@pytest.mark.parametrize("model, expected_model", [(None, "inherited-model"), ("chosen-model", "chosen-model")])
def test_vendor_driver_selects_configured_model_or_preserves_inherited_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["codex", "claude"],
    model: str | None,
    expected_model: str,
) -> None:
    monkeypatch.setenv("SIXNIMMT_TEST_MODEL", "inherited-model")
    source = (
        "import json, os, pathlib, sys\n"
        "sys.stdin.read()\n"
        "arguments = sys.argv[1:]\n"
        "if 'exec' in arguments:\n"
        "    arguments = arguments[arguments.index('exec') + 1:]\n"
        "selected_model = os.environ['SIXNIMMT_TEST_MODEL']\n"
        "if '--model' in arguments:\n"
        "    selected_model = arguments[arguments.index('--model') + 1]\n"
        "proposal = {'selected_model': selected_model}\n"
        "if '--output-last-message' in arguments:\n"
        "    result = pathlib.Path(arguments[arguments.index('--output-last-message') + 1])\n"
        "    result.write_text(json.dumps(proposal))\n"
        "else:\n"
        "    print(json.dumps({'subtype': 'success', 'structured_output': proposal}))\n"
    )
    driver = make_driver(tmp_path, source, kind=kind, model=model)

    assert driver.invoke({}, {}, {}) == {"selected_model": expected_model}


@pytest.mark.parametrize("kind", ["codex", "claude"])
@pytest.mark.parametrize(("effort", "expected"), [(None, "low"), ("high", "high")])
def test_vendor_effort_option_overrides_inherited_default_only_for_its_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Literal["codex", "claude"], effort: str | None, expected: str
) -> None:
    monkeypatch.setenv("SIXNIMMT_TEST_EFFORT", "low")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    source = (
        "import json, os, pathlib, sys, tomllib\n"
        "sys.stdin.read()\n"
        "arguments = sys.argv[1:]\n"
        "if 'exec' in arguments:\n"
        "    selected = os.environ['SIXNIMMT_TEST_EFFORT']\n"
        "    if '-c' in arguments:\n"
        "        selected = tomllib.loads(arguments[arguments.index('-c') + 1])['model_reasoning_effort']\n"
        "    result = pathlib.Path(arguments[arguments.index('--output-last-message') + 1])\n"
        "    result.write_text(json.dumps({'selected_effort': selected}))\n"
        "else:\n"
        "    selected = os.environ['CLAUDE_CODE_EFFORT_LEVEL']\n"
        "    if '--effort' in arguments:\n"
        "        assert arguments[arguments.index('--effort') + 1] == selected\n"
        "    print(json.dumps({'subtype': 'success', 'structured_output': {'selected_effort': selected}}))\n"
    )
    driver = make_driver(tmp_path, source, kind=kind, reasoning_effort=effort)
    records: list[dict[str, Any]] = []
    driver.set_trace(records.append)
    assert driver.invoke({}, {}, {}) == {"selected_effort": expected}
    assert all(record["reasoning_effort"] == effort for record in records)
    assert os.environ["CLAUDE_CODE_EFFORT_LEVEL"] == "low"


@pytest.mark.parametrize("response", [{"subtype": "error", "structured_output": {}}, {"subtype": "success"}])
def test_claude_rejects_unsuccessful_or_unstructured_result(tmp_path: Path, response: dict[str, object]) -> None:
    driver = make_driver(tmp_path, f"print({json.dumps(response)!r})\n", kind="claude")
    with pytest.raises(DriverError):
        driver.invoke({}, {}, {})


def test_vendor_schema_requires_nullable_recipient_and_preserves_broker_schema() -> None:
    schema = structured_proposal_schema(PROPOSAL_SCHEMA)
    message = schema["$defs"]["SendMessageAction"]
    assert "to_player" in message["required"]
    assert message["properties"]["to_player"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert "to_player" not in PROPOSAL_SCHEMA["$defs"]["SendMessageAction"]["required"]
    actions = schema["properties"]["actions"]["items"]
    assert "anyOf" in actions
    assert "oneOf" not in actions
    assert "discriminator" not in actions


@pytest.mark.skipif(os.name != "posix", reason="process groups are a POSIX execution profile")
def test_successful_parent_does_not_leave_child_running(tmp_path: Path) -> None:
    marker = tmp_path / "child-finished"
    child = f"import pathlib, time; time.sleep(0.7); pathlib.Path({str(marker)!r}).touch()"
    source = f"import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', {child!r}])\nprint('{{}}')\n"
    driver = make_driver(tmp_path, source)
    assert driver.invoke({}, {}, {}) == {}
    time.sleep(0.9)
    assert not marker.exists()


class _ActivityRecorder:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.received_text = Event()

    def __call__(self, record: dict[str, Any]) -> None:
        self.records.append(record)
        if record["type"] == "model_text":
            self.received_text.set()


class _FailingActivityRecorder:
    def __init__(self, failure_type: str) -> None:
        self.failure_type = failure_type
        self.failed = False
        self.error = RuntimeError("observer unavailable")

    def __call__(self, record: dict[str, Any]) -> None:
        if record["type"] == self.failure_type:
            self.failed = True
        if self.failed:
            raise self.error


@pytest.mark.parametrize("stream, failure_type", [("stdout", "model_text"), ("stderr", "stderr")])
def test_callback_failure_keeps_draining_child_output_and_is_raised_after_cleanup(
    tmp_path: Path, stream: str, failure_type: str
) -> None:
    finished = tmp_path / "finished"
    event = {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "Choosing"}}
    source = (
        "import json, pathlib, sys\n"
        "sys.stdin.read()\n"
        f"print({json.dumps(event)!r}, file=sys.{stream}, flush=True)\n"
        "for _ in range(128):\n"
        f"    print(json.dumps({{'type': 'diagnostic', 'text': 'x' * 4096}}), file=sys.{stream}, flush=True)\n"
        f"pathlib.Path({str(finished)!r}).write_text('finished')\n"
        "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('{}')\n"
    )
    driver = make_driver(tmp_path, source, kind="codex", timeout_seconds=5)
    observer = _FailingActivityRecorder(failure_type)
    driver.set_trace(observer)
    with pytest.raises(DriverError, match=f"managed {stream} progress callback failed") as caught:
        driver.invoke({}, {}, {})
    assert caught.value.__cause__ is observer.error
    assert finished.read_text() == "finished"
    assert list((tmp_path / "workspace").iterdir()) == []


@pytest.mark.parametrize(
    "kind, event",
    [
        ("codex", {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "Choosing a card"}}),
        (
            "claude",
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Choosing a card"},
                },
            },
        ),
    ],
)
def test_vendor_activity_arrives_before_process_exits(
    tmp_path: Path, kind: Literal["codex", "claude"], event: dict[str, Any]
) -> None:
    release = tmp_path / "release"
    source = (
        "import json, pathlib, sys, time\n"
        "sys.stdin.read()\n"
        f"print({json.dumps(event)!r}, flush=True)\n"
        f"while not pathlib.Path({str(release)!r}).exists():\n"
        "    time.sleep(0.01)\n"
        "if '--output-last-message' in sys.argv:\n"
        "    pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('{}')\n"
        "else:\n"
        "    print(json.dumps({'type': 'result', 'subtype': 'success', 'structured_output': {}}))\n"
    )
    driver = make_driver(tmp_path, source, kind=kind)
    recorder = _ActivityRecorder()
    driver.set_trace(recorder)
    errors: list[Exception] = []
    thread = Thread(target=_invoke_and_capture, args=(driver, errors))
    thread.start()
    try:
        assert recorder.received_text.wait(timeout=3)
        assert thread.is_alive()
        release.touch()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert errors == []
    finally:
        driver.cancel()
        thread.join(timeout=3)
    assert recorder.records[-1]["type"] == "invocation_completed"


def test_claude_stream_accepts_only_final_result_and_ignores_trailing_metadata(tmp_path: Path) -> None:
    records = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": '{"submission_id":"interim"}'}]}},
        {"type": "result", "subtype": "success", "structured_output": {"submission_id": "final"}},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
    ]
    source = "\n".join(f"print({json.dumps(record)!r})" for record in records)
    driver = make_driver(tmp_path, source, kind="claude")
    assert driver.invoke({}, {}, {}) == {"submission_id": "final"}


@pytest.mark.parametrize(
    "records",
    [
        [{"type": "assistant", "structured_output": {"submission_id": "interim"}}],
        [
            {"type": "result", "subtype": "success", "structured_output": {}},
            {"type": "result", "subtype": "success", "structured_output": {}},
        ],
        [{"type": "result", "subtype": "success", "is_error": True, "structured_output": {}}],
    ],
)
def test_claude_stream_rejects_missing_ambiguous_or_failed_final_result(
    tmp_path: Path, records: list[dict[str, Any]]
) -> None:
    source = "\n".join(f"print({json.dumps(record)!r})" for record in records)
    driver = make_driver(tmp_path, source, kind="claude")
    with pytest.raises(DriverError):
        driver.invoke({}, {}, {})


def test_claude_requests_incremental_json_output(tmp_path: Path) -> None:
    source = (
        "import json, sys\n"
        "assert sys.argv[sys.argv.index('--output-format') + 1] == 'stream-json'\n"
        "assert '--verbose' in sys.argv\n"
        "assert '--include-partial-messages' in sys.argv\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'structured_output': {}}))\n"
    )
    assert make_driver(tmp_path, source, kind="claude").invoke({}, {}, {}) == {}


@pytest.mark.parametrize(
    "ending, message", [("sys.exit(3)", "status 3"), ("time.sleep(10)", "managed_decision_timeout")]
)
def test_failure_trace_retains_bounded_stdout_and_stderr_before_cleanup(
    tmp_path: Path, ending: str, message: str
) -> None:
    source = f"import sys, time\nprint('partial response', flush=True)\nprint('provider diagnostic', file=sys.stderr, flush=True)\n{ending}\n"
    driver = make_driver(tmp_path, source, timeout_seconds=0.3)
    records: list[dict[str, Any]] = []
    driver.set_trace(records.append)
    with pytest.raises(DriverError, match=message):
        driver.invoke({"decision_id": "decision-1", "view_id": "view-1"}, {}, {})
    failure = records[-1]
    assert failure["type"] == "invocation_failed"
    assert failure["stdout"] == "partial response\n"
    assert failure["stderr"] == "provider diagnostic\n"
    assert failure["decision_id"] == "decision-1"
    assert all(record["view_id"] == "view-1" for record in records)
    assert failure["invocation"] == 1
    assert list((tmp_path / "workspace").iterdir()) == []


def test_malformed_final_file_is_retained_in_private_trace(tmp_path: Path) -> None:
    source = (
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('not a proposal')\n"
    )
    driver = make_driver(tmp_path, source, kind="codex")
    records: list[dict[str, Any]] = []
    driver.set_trace(records.append)
    with pytest.raises(DriverError, match="complete JSON object"):
        driver.invoke({}, {}, {})
    output = next(record for record in records if record["type"] == "invocation_output")
    assert output["text"] == "not a proposal"
    assert records[-1]["type"] == "invocation_failed"


def test_stdout_and_stderr_share_one_output_budget(tmp_path: Path) -> None:
    driver = make_driver(
        tmp_path,
        "import sys\nprint('a' * 100, flush=True)\nprint('b' * 100, file=sys.stderr, flush=True)\n",
        max_output_bytes=150,
    )
    records: list[dict[str, Any]] = []
    driver.set_trace(records.append)
    with pytest.raises(DriverError, match="managed_output_limit"):
        driver.invoke({}, {}, {})
    failure = records[-1]
    assert len(failure["stdout"].encode()) + len(failure["stderr"].encode()) == 150


@pytest.mark.skipif(os.name != "posix", reason="process groups are a POSIX execution profile")
def test_exiting_group_signal_denial_preserves_output_limit_and_captured_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "release"
    finished = tmp_path / "finished"
    source = (
        "import pathlib, sys, time\n"
        "print('a' * 100, flush=True)\n"
        "print('b' * 100, file=sys.stderr, flush=True)\n"
        f"while not pathlib.Path({str(release)!r}).exists():\n"
        "    time.sleep(0.001)\n"
        f"pathlib.Path({str(finished)!r}).touch()\n"
    )
    original_killpg = os.killpg

    def deny_first_signal(group: int, signum: int) -> None:
        if not release.exists():
            release.touch()
            message = "group is exiting"
            raise PermissionError(message)
        original_killpg(group, signum)

    monkeypatch.setattr(os, "killpg", deny_first_signal)
    driver = make_driver(tmp_path, source, max_output_bytes=150)
    records: list[dict[str, Any]] = []
    driver.set_trace(records.append)
    with pytest.raises(DriverError, match="managed_output_limit"):
        driver.invoke({}, {}, {})
    assert finished.exists()
    assert len(records[-1]["stdout"].encode()) + len(records[-1]["stderr"].encode()) == 150
    assert list((tmp_path / "workspace").iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="process groups are a POSIX execution profile")
def test_denied_graceful_signal_still_kills_a_running_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_killpg = os.killpg
    signals: list[int] = []

    def deny_graceful_signal(group: int, signum: int) -> None:
        signals.append(signum)
        if signum == signal.SIGTERM:
            message = "graceful signal denied"
            raise PermissionError(message)
        original_killpg(group, signum)

    monkeypatch.setattr(os, "killpg", deny_graceful_signal)
    source = "import time\nprint('a' * 200, flush=True)\ntime.sleep(10)\n"
    driver = make_driver(tmp_path, source, max_output_bytes=150)
    before = time.monotonic()
    with pytest.raises(DriverError, match="managed_output_limit"):
        driver.invoke({}, {}, {})
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert time.monotonic() - before < 3


@pytest.mark.parametrize(
    "source, expected_error",
    [("print('a' * 200, flush=True)\n", "managed_output_limit"), ("print('{}')\n", "managed process cleanup failed")],
)
def test_cleanup_failure_is_reported_without_replacing_an_existing_invocation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, expected_error: str
) -> None:
    original_reap = harness_drivers._reap
    cleanup_error = PermissionError("cleanup reporting failed")

    def failed_cleanup(process: subprocess.Popen[bytes]) -> None:
        original_reap(process)
        raise cleanup_error

    monkeypatch.setattr(harness_drivers, "_reap", failed_cleanup)
    driver = make_driver(tmp_path, source, max_output_bytes=150)
    records: list[dict[str, Any]] = []
    driver.set_trace(records.append)
    with pytest.raises(DriverError, match=expected_error) as caught:
        driver.invoke({}, {}, {})
    assert caught.value.__cause__ is cleanup_error
    assert records[-2]["type"] == "cleanup_failed"
    assert records[-1]["type"] == "invocation_failed"
    assert records[-1]["stdout"] != ""
    assert list((tmp_path / "workspace").iterdir()) == []


def test_failure_diagnostics_keep_bounded_beginning_and_end_of_large_output(tmp_path: Path) -> None:
    driver = make_driver(tmp_path, "import sys\nprint('start' + 'a' * 40000 + 'end')\nsys.exit(3)\n")
    records: list[dict[str, Any]] = []
    driver.set_trace(records.append)
    with pytest.raises(DriverError, match="status 3"):
        driver.invoke({}, {}, {})
    output = records[-1]["stdout"]
    assert records[-1]["stdout_truncated"] is True
    assert len(output.encode()) <= 32768
    assert output.startswith("start")
    assert output.endswith("end\n")


def test_trace_redacts_known_credentials_in_output_and_nested_proposals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = "test-secret-provider-credential"
    monkeypatch.setenv("ANTHROPIC_API_KEY", credential)
    source = "import os, sys\nprint('{}')\nprint(os.environ['ANTHROPIC_API_KEY'], file=sys.stderr, flush=True)\n"
    driver = make_driver(tmp_path, source)
    records: list[dict[str, Any]] = []
    driver.set_trace(records.append)
    assert driver.invoke({}, {}, {}) == {}
    driver.emit_trace({"type": "proposal", "proposal": {"memory": credential}})
    encoded = json.dumps(records)
    assert credential not in encoded
    assert "[redacted]" in encoded


def test_trace_redacts_credentials_split_between_streamed_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = "test-secret-provider-credential"
    monkeypatch.setenv("OPENAI_API_KEY", credential)
    driver = make_driver(tmp_path, "print('{}')\n")
    records: list[dict[str, Any]] = []
    driver.set_trace(records.append)
    assert driver.invoke({}, {}, {}) == {}
    driver.emit_trace({"type": "model_text", "text": credential[:12], "item_id": "a"})
    driver.emit_trace({"type": "model_text", "text": credential[12:], "item_id": "a"})
    text = "".join(record["text"] for record in records if record["type"] == "model_text")
    assert text == "[redacted]"


def test_text_completion_survives_redaction_even_without_new_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret-provider-credential")
    driver = make_driver(tmp_path, "print('{}')\n")
    records: list[dict[str, Any]] = []
    driver.set_trace(records.append)
    assert driver.invoke({"view_id": "view-1"}, {}, {}) == {}
    driver.emit_trace({"type": "model_text", "text": "test-secret-", "item_id": "a", "complete": False})
    driver.emit_trace({"type": "model_text", "text": "", "item_id": "a", "complete": True})
    assert records[-1]["text"] == "[redacted]"
    assert records[-1]["complete"] is True
    assert records[-1]["view_id"] == "view-1"
    driver.emit_trace({"type": "model_text", "text": "", "item_id": "b", "complete": True})
    assert records[-1]["text"] == ""
    assert records[-1]["item_id"] == "b"
    assert records[-1]["complete"] is True
