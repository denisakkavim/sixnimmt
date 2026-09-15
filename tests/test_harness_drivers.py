"""Managed harnesses consume one request and publish only completed final JSON."""

import json
import os
import sys
import time
from pathlib import Path
from threading import Thread
from typing import Literal

import pytest

from sixnimmt.arena.bots.external_harnesses.drivers import (
    CommandProfile,
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
) -> ManagedCommandDriver:
    script = tmp_path / "agent.py"
    script.write_text(source, encoding="utf-8")
    profile = CommandProfile((sys.executable, str(script)), kind, timeout_seconds, max_output_bytes)
    return ManagedCommandDriver(profile, tmp_path / "workspace")


def test_generic_driver_receives_filtered_offer_and_returns_final_object(tmp_path: Path) -> None:
    driver = make_driver(
        tmp_path,
        "import json, sys\nrequest = json.load(sys.stdin)\nprint(json.dumps(request['offer']))\n",
    )
    result = driver.invoke({"decision_id": "decision-1"}, {"rules": "game instructions"}, {})
    assert result == {"decision_id": "decision-1"}
    assert list((tmp_path / "workspace").iterdir()) == []


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
    with pytest.raises(DriverError, match="managed_decision_timeout"):
        driver.invoke({}, {}, {})
    assert time.monotonic() - before < 3


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


@pytest.mark.parametrize("response", [{"subtype": "error", "structured_output": {}}, {"subtype": "success"}])
def test_claude_rejects_unsuccessful_or_unstructured_result(tmp_path: Path, response: dict[str, object]) -> None:
    driver = make_driver(tmp_path, f"print({json.dumps(response)!r})\n", kind="claude")
    with pytest.raises(DriverError):
        driver.invoke({}, {}, {})


def test_vendor_schema_requires_nullable_recipient_and_preserves_broker_schema() -> None:
    schema = structured_proposal_schema(PROPOSAL_SCHEMA)
    message = schema["$defs"]["_SendMessage"]
    assert "to_player" in message["required"]
    assert message["properties"]["to_player"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert "to_player" not in PROPOSAL_SCHEMA["$defs"]["_SendMessage"]["required"]
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
