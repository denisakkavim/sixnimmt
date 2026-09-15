"""Bounded, fresh-process JSON adapters for operator-selected commands."""

import codecs
import json
import math
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from typing import IO, Any, Literal

from sixnimmt.arena.bots.external_harnesses.streaming import VendorEvents

_DIAGNOSTIC_BYTES = 32_768


class DriverError(RuntimeError):
    """A managed invocation failed before producing a final proposal."""


class DriverFormatError(DriverError):
    """A successfully completed invocation returned an unusable final payload."""


@dataclass(frozen=True)
class CommandProfile:
    """Execution settings for a fresh decision; commands never use a shell."""

    command: tuple[str, ...]
    kind: Literal["command", "codex", "claude"] = "command"
    timeout_seconds: float = 120.0
    max_output_bytes: int = 1_048_576
    model: str | None = None
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if len(self.command) == 0 or any(argument == "" for argument in self.command):
            msg = "managed command requires a nonempty argument vector"
            raise ValueError(msg)
        if self.kind not in ("command", "codex", "claude"):
            msg = "unknown managed command profile"
            raise ValueError(msg)
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            msg = "managed timeout must be finite and positive"
            raise ValueError(msg)
        if type(self.max_output_bytes) is not int or self.max_output_bytes < 1:
            msg = "managed output limit must be a positive integer"
            raise ValueError(msg)
        if self.model is not None:
            if not isinstance(self.model, str) or self.model.strip() == "":
                msg = "managed model must be a nonempty string"
                raise ValueError(msg)
            if self.kind == "command":
                msg = "model is only supported for Codex and Claude command profiles"
                raise ValueError(msg)
        self._validate_reasoning_effort()

    def _validate_reasoning_effort(self) -> None:
        effort = self.reasoning_effort
        if effort is None:
            return
        if not isinstance(effort, str) or effort.strip() == "":
            msg = "reasoning_effort must be a nonempty string"
            raise ValueError(msg)
        if self.kind == "command":
            msg = "reasoning_effort is only supported for Codex and Claude command profiles"
            raise ValueError(msg)
        if self.kind == "claude" and effort not in ("low", "medium", "high", "xhigh", "max"):
            msg = "Claude reasoning_effort must be low, medium, high, xhigh, or max"
            raise ValueError(msg)


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            msg = "managed output contains duplicate JSON keys"
            raise DriverFormatError(msg)
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    msg = "managed output contains a non-finite JSON number"
    raise DriverFormatError(msg)


def parse_final_json(contents: bytes) -> dict[str, Any]:
    """Accept exactly one complete object, never a transcript or JSON fragment."""
    try:
        result = json.loads(contents, object_pairs_hook=_object_pairs, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError) as error:
        msg = "managed output must be one complete JSON object"
        raise DriverFormatError(msg) from error
    if not isinstance(result, dict):
        msg = "managed output must be a JSON object"
        raise DriverFormatError(msg)
    return result


def _claude_proposal(contents: bytes) -> dict[str, Any]:
    result: dict[str, Any] | None = None
    for line in contents.splitlines():
        if line.strip() == b"":
            continue
        record = parse_final_json(line)
        # Accept the old single JSON envelope as well as stream-json. Other
        # assistant, tool and trailing metadata records are never proposals.
        is_result = record.get("type") == "result" or ("type" not in record and "subtype" in record)
        if not is_result:
            continue
        if result is not None:
            msg = "Claude invocation returned multiple final results"
            raise DriverFormatError(msg)
        result = record
    if result is None:
        msg = "Claude invocation omitted its final result"
        raise DriverFormatError(msg)
    if result.get("is_error") is True or result.get("subtype") != "success":
        msg = "Claude invocation did not report successful completion"
        raise DriverError(msg)
    proposal = result.get("structured_output")
    if not isinstance(proposal, dict):
        msg = "Claude invocation omitted structured_output"
        raise DriverFormatError(msg)
    return proposal


def _diagnostic_output(key: str, contents: bytes) -> dict[str, Any]:
    truncated = len(contents) > _DIAGNOSTIC_BYTES
    if truncated:
        marker = b"\n[... output truncated ...]\n"
        half = (_DIAGNOSTIC_BYTES - len(marker)) // 2
        contents = contents[:half] + marker + contents[-half:]
    return {key: contents.decode("utf-8", errors="replace"), f"{key}_truncated": truncated}


def _redact(value: Any, credentials: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for credential in credentials:
            value = value.replace(credential, "[redacted]")
        return value
    if isinstance(value, dict):
        return {key: _redact(item, credentials) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, credentials) for item in value]
    return value


def structured_proposal_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Use the vendors' strict structured-output subset without changing validation."""
    result = deepcopy(schema)
    _strict_schema_node(result)
    return result


def _strict_schema_node(node: Any) -> None:
    if isinstance(node, list):
        for value in node:
            _strict_schema_node(value)
        return
    if not isinstance(node, dict):
        return
    node.pop("default", None)
    node.pop("discriminator", None)
    if "oneOf" in node:
        node["anyOf"] = node.pop("oneOf")
    if "const" in node:
        node["enum"] = [node.pop("const")]
    properties = node.get("properties")
    if isinstance(properties, dict):
        node["required"] = list(properties)
        node["additionalProperties"] = False
    for value in node.values():
        _strict_schema_node(value)


class _OutputBudget:
    def __init__(self, limit: int) -> None:
        self.remaining = limit
        self.exceeded = Event()
        self.lock = Lock()

    def accept(self, chunk: bytes) -> bytes:
        with self.lock:
            accepted = chunk[: self.remaining]
            self.remaining -= len(accepted)
            if len(accepted) < len(chunk):
                self.exceeded.set()
            return accepted


class _Capture:
    def __init__(
        self, stream: IO[bytes], budget: _OutputBudget, receive: Callable[[bytes], None] | None = None
    ) -> None:
        self.stream = stream
        self.budget = budget
        self.receive = receive
        self.contents = bytearray()
        self.receive_error: Exception | None = None

    def drain(self) -> None:
        try:
            while True:
                # Buffered read(size) can wait to fill size even after an event
                # is flushed. os.read returns whatever the pipe has available.
                chunk = os.read(self.stream.fileno(), 8192)
                if len(chunk) == 0:
                    return
                accepted = self.budget.accept(chunk)
                self.contents.extend(accepted)
                self._notify(accepted)
        finally:
            self.stream.close()

    def _notify(self, chunk: bytes) -> None:
        if self.receive is None or self.receive_error is not None or len(chunk) == 0:
            return
        try:
            self.receive(chunk)
        except Exception as error:
            # The observer must not break the child's pipe or stop consuming
            # output. Report its first error on the invocation thread instead.
            self.receive_error = error

    def finish(self, callback: Callable[[], None]) -> None:
        if self.receive_error is not None:
            return
        try:
            callback()
        except Exception as error:
            self.receive_error = error


def _feed(stream: IO[bytes], payload: bytes) -> None:
    try:
        stream.write(payload)
        stream.flush()
    except (BrokenPipeError, OSError):
        return
    finally:
        stream.close()


def _signal_process(process: subprocess.Popen[bytes], signum: int) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signum)
        elif process.poll() is None:
            process.send_signal(signum)
    except ProcessLookupError:
        return
    except PermissionError as error:
        # macOS can deny signalling an exiting group before waitpid observes
        # its leader's exit. Allow that short race without hiding a live child.
        try:
            process.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            raise error from None


def _reap(process: subprocess.Popen[bytes]) -> None:
    # A successful parent can leave descendants holding its pipes open. The
    # process group belongs to this invocation, even after its leader exits.
    # Still attempt SIGKILL if graceful signalling was denied; the invocation
    # owns the group, including descendants after its leader has exited.
    with suppress(OSError):
        _signal_process(process, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=0.5)
    _signal_process(process, signal.SIGKILL)
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired as error:
        msg = "managed process cleanup timed out"
        raise DriverError(msg) from error


def _reap_and_join(process: subprocess.Popen[bytes], threads: list[Thread]) -> Exception | None:
    try:
        _reap(process)
    except (OSError, DriverError) as error:
        return error
    finally:
        for thread in threads:
            thread.join(timeout=1)
    return None


class ManagedCommandDriver:
    """Own one process tree per decision and accept only its final JSON result."""

    def __init__(self, profile: CommandProfile, workspace: Path) -> None:
        self.profile = profile
        self.workspace = workspace
        self._cancelled = Event()
        self._trace: Callable[[dict[str, Any]], None] | None = None
        self._trace_lock = Lock()
        self._last_stdout = b""
        self._last_stderr = b""
        self._stderr_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._decision_id: str | None = None
        self._credentials: tuple[str, ...] = ()
        self._pending_text: dict[tuple[str, str], dict[str, Any]] = {}
        self.invocations = 0

    def set_trace(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        """Attach the private model trace, including live progress and failures."""
        self._trace = callback

    def emit_trace(self, record: dict[str, Any]) -> None:
        """Add invocation metadata without logging command arguments or environment."""
        with self._trace_lock:
            if self._trace is None:
                return
            redacted = self._redact_stream(record)
            if redacted is None:
                return
            event = {
                "trace_version": 1,
                "client": self.profile.kind,
                "model": self.profile.model,
                "reasoning_effort": self.profile.reasoning_effort,
                "invocation": self.invocations,
                "decision_id": self._decision_id,
                "timestamp": datetime.now(UTC).isoformat(),
                **redacted,
            }
            self._trace(_redact(event, self._credentials))

    def _redact_stream(self, record: dict[str, Any]) -> dict[str, Any] | None:
        kind = record.get("type")
        text = record.get("text")
        if kind not in ("model_text", "reasoning_summary", "stderr") or not isinstance(text, str):
            return record
        key = (str(kind), str(record.get("item_id", "")))
        previous = self._pending_text.pop(key, None)
        if previous is not None:
            text = previous["text"] + text
        text = _redact(text, self._credentials)
        # Hold only a possible credential prefix between streamed chunks. A
        # secret split across pipe reads must not escape through live tracing.
        pending = 0
        for credential in self._credentials:
            for length in range(1, min(len(credential), len(text) + 1)):
                if text.endswith(credential[:length]):
                    pending = max(pending, length)
        if pending > 0:
            self._pending_text[key] = {**record, "text": text[-pending:]}
            text = text[:-pending]
        if text == "":
            return None
        return {**record, "text": text}

    def _flush_text(self) -> None:
        pending = list(self._pending_text.values())
        self._pending_text.clear()
        for record in pending:
            self.emit_trace({**record, "text": "[redacted]"})

    def cancel(self) -> None:
        self._cancelled.set()

    def close(self) -> None:
        self.cancel()

    def invoke(
        self,
        offer: dict[str, Any],
        game_info: dict[str, Any],
        schema: dict[str, Any],
        *,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Deliver the filtered offer and return a proposal for broker validation."""
        if self._cancelled.is_set():
            msg = "managed_invocation_cancelled"
            raise DriverError(msg)
        self.invocations += 1
        self._decision_id = offer.get("decision_id")
        self._last_stdout = b""
        self._last_stderr = b""
        self._stderr_decoder.reset()
        self._pending_text.clear()
        self._credentials = tuple(
            value
            for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "SIXNIMMT_CREDENTIAL")
            if (value := os.environ.get(name)) is not None and value != ""
        )
        started = time.monotonic()
        invocation_deadline = started + self.profile.timeout_seconds
        if deadline is not None:
            invocation_deadline = min(invocation_deadline, deadline)
        deadline_utc = datetime.now(UTC) + timedelta(seconds=invocation_deadline - started)
        self.emit_trace({"type": "invocation_started", "deadline": deadline_utc.isoformat()})
        try:
            proposal = self._invoke(offer, game_info, schema, invocation_deadline)
        except (DriverError, OSError) as error:
            message = str(error) if isinstance(error, DriverError) else "managed process I/O failed"
            # A failing observer cannot replace the original invocation error
            # while we try to report that failure through the same observer.
            with suppress(Exception):
                self._flush_text()
                self.emit_trace({
                    "type": "invocation_failed",
                    "error": message,
                    "elapsed_seconds": time.monotonic() - started,
                    **_diagnostic_output("stdout", self._last_stdout),
                    **_diagnostic_output("stderr", self._last_stderr),
                })
            if isinstance(error, DriverError):
                raise
            raise DriverError(message) from error
        self._flush_text()
        self.emit_trace({"type": "invocation_completed", "elapsed_seconds": time.monotonic() - started})
        return proposal

    def _invoke(
        self,
        offer: dict[str, Any],
        game_info: dict[str, Any],
        schema: dict[str, Any],
        deadline: float | None,
    ) -> dict[str, Any]:
        self.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="decision-", dir=self.workspace) as directory:
            scratch = Path(directory)
            command, final_path = self._command(scratch, schema)
            request = {"game_info": game_info, "offer": offer}
            payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode() + b"\n"
            if self.profile.kind != "command":
                payload = (
                    b"Play this 6 nimmt! decision using the supplied rules and observation. "
                    b"You may give brief commentary while deciding. Your final response must contain "
                    b"only the proposal matching the JSON schema. Copy the session, "
                    b"decision and view IDs exactly; choose a fresh unique submission_id.\n" + payload
                )
            output = self._execute(command, payload, scratch, deadline)
            if final_path is not None:
                if not final_path.is_file():
                    msg = "managed invocation omitted its final result file"
                    raise DriverFormatError(msg)
                if final_path.stat().st_size > self.profile.max_output_bytes:
                    msg = "managed_output_limit"
                    raise DriverError(msg)
                output = final_path.read_bytes()
            self.emit_trace({"type": "invocation_output", **_diagnostic_output("text", output)})
            if self.profile.kind == "claude":
                return _claude_proposal(output)
            return parse_final_json(output)

    def _command(self, scratch: Path, schema: dict[str, Any]) -> tuple[list[str], Path | None]:
        command = list(self.profile.command)
        if self.profile.kind != "command":
            schema = structured_proposal_schema(schema)
        if self.profile.kind == "codex":
            schema_path = scratch / "proposal.schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            result_path = scratch / "proposal.json"
            command.append("exec")
            if self.profile.model is not None:
                command.extend(["--model", self.profile.model])
            if self.profile.reasoning_effort is not None:
                command.extend(["-c", "model_reasoning_effort=" + json.dumps(self.profile.reasoning_effort)])
            command.extend([
                "--ephemeral",
                "--skip-git-repo-check",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(result_path),
                "--json",
                "-",
            ])
            return command, result_path
        if self.profile.kind == "claude":
            command.extend([
                "-p",
                "--no-session-persistence",
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--json-schema",
                json.dumps(schema),
            ])
            if self.profile.model is not None:
                command.extend(["--model", self.profile.model])
            if self.profile.reasoning_effort is not None:
                command.extend(["--effort", self.profile.reasoning_effort])
        return command, None

    def _execute(self, command: list[str], payload: bytes, scratch: Path, deadline: float | None) -> bytes:
        invocation_deadline = time.monotonic() + self.profile.timeout_seconds
        if deadline is not None:
            invocation_deadline = min(invocation_deadline, deadline)
        if invocation_deadline <= time.monotonic():
            msg = "managed_decision_timeout"
            raise DriverError(msg)
        environment = None
        if self.profile.kind == "claude" and self.profile.reasoning_effort is not None:
            # Claude gives its environment effort setting precedence over saved
            # choices. An explicit seat option must also override that default.
            environment = {**os.environ, "CLAUDE_CODE_EFFORT_LEVEL": self.profile.reasoning_effort}
        process = subprocess.Popen(  # noqa: S603 - operator-configured argument vector, no shell.
            command,
            cwd=scratch,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            _reap(process)
            msg = "managed process pipes were not created"
            raise DriverError(msg)
        budget = _OutputBudget(self.profile.max_output_bytes)
        events = VendorEvents(self.profile.kind, self.emit_trace)
        stdout = _Capture(process.stdout, budget, events.feed if self.profile.kind != "command" else None)
        stderr = _Capture(process.stderr, budget, self._stderr)
        threads = [
            Thread(target=stdout.drain, daemon=True),
            Thread(target=stderr.drain, daemon=True),
            Thread(target=_feed, args=(process.stdin, payload), daemon=True),
        ]
        for thread in threads:
            thread.start()
        invocation_error: DriverError | None = None
        try:
            self._wait(process, budget.exceeded, invocation_deadline)
        except DriverError as error:
            invocation_error = error
        finally:
            cleanup_error = _reap_and_join(process, threads)
            self._last_stdout = bytes(stdout.contents)
            self._last_stderr = bytes(stderr.contents)
            stdout.finish(events.finish)
            stderr.finish(self._finish_stderr)
        self._check_execution_errors(invocation_error, cleanup_error)
        if budget.exceeded.is_set():
            msg = "managed_output_limit"
            raise DriverError(msg)
        for name, capture in (("stdout", stdout), ("stderr", stderr)):
            if capture.receive_error is not None:
                msg = f"managed {name} progress callback failed ({type(capture.receive_error).__name__})"
                raise DriverError(msg) from capture.receive_error
        return bytes(stdout.contents)

    def _check_execution_errors(self, invocation_error: DriverError | None, cleanup_error: Exception | None) -> None:
        if cleanup_error is None:
            if invocation_error is not None:
                raise invocation_error
            return
        with suppress(Exception):
            self.emit_trace({
                "type": "cleanup_failed",
                "error": f"managed process cleanup failed ({type(cleanup_error).__name__})",
            })
        if invocation_error is not None:
            raise invocation_error from cleanup_error
        msg = "managed process cleanup failed"
        raise DriverError(msg) from cleanup_error

    def _finish_stderr(self) -> None:
        self._stderr(b"", final=True)

    def _stderr(self, chunk: bytes, *, final: bool = False) -> None:
        text = self._stderr_decoder.decode(chunk, final=final)
        if text != "":
            self.emit_trace({"type": "stderr", "text": text})

    def _wait(self, process: subprocess.Popen[bytes], exceeded: Event, deadline: float) -> None:
        while True:
            if self._cancelled.is_set():
                msg = "managed_invocation_cancelled"
                raise DriverError(msg)
            if exceeded.is_set():
                msg = "managed_output_limit"
                raise DriverError(msg)
            if time.monotonic() >= deadline:
                msg = "managed_decision_timeout"
                raise DriverError(msg)
            status = process.poll()
            if status is not None:
                if status != 0:
                    msg = f"managed process exited with status {status}"
                    raise DriverError(msg)
                return
            self._cancelled.wait(0.02)
