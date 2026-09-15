"""Bounded, fresh-process JSON adapters for operator-selected commands."""

import json
import math
import os
import signal
import subprocess
import tempfile
import time
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import IO, Any, Literal


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


class _Capture:
    def __init__(self, stream: IO[bytes], limit: int, exceeded: Event) -> None:
        self.stream = stream
        self.limit = limit
        self.exceeded = exceeded
        self.contents = bytearray()

    def drain(self) -> None:
        try:
            while True:
                chunk = self.stream.read(8192)
                if len(chunk) == 0:
                    return
                remaining = self.limit - len(self.contents)
                self.contents.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.exceeded.set()
        finally:
            self.stream.close()


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
    except PermissionError:
        # Some macOS sandboxes deny a second signal to a group whose last
        # process has already exited, rather than reporting ESRCH.
        if process.poll() is None:
            raise


def _reap(process: subprocess.Popen[bytes]) -> None:
    # A successful parent can leave descendants holding its pipes open. The
    # process group belongs to this invocation, even after its leader exits.
    _signal_process(process, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=0.5)
    _signal_process(process, signal.SIGKILL)
    process.wait()


class ManagedCommandDriver:
    """Own one process tree per decision and accept only its final JSON result."""

    def __init__(self, profile: CommandProfile, workspace: Path) -> None:
        self.profile = profile
        self.workspace = workspace
        self._cancelled = Event()
        self.invocations = 0

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
        self.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="decision-", dir=self.workspace) as directory:
            scratch = Path(directory)
            command, final_path = self._command(scratch, schema)
            request = {"game_info": game_info, "offer": offer}
            payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode() + b"\n"
            if self.profile.kind != "command":
                payload = (
                    b"Play this 6 nimmt! decision using the supplied rules and observation. "
                    b"Return only the final proposal matching the JSON schema. Copy the session, "
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
            result = parse_final_json(output)
            if self.profile.kind == "claude":
                if result.get("is_error") is True or result.get("subtype") != "success":
                    msg = "Claude invocation did not report successful completion"
                    raise DriverError(msg)
                proposal = result.get("structured_output")
                if not isinstance(proposal, dict):
                    msg = "Claude invocation omitted structured_output"
                    raise DriverFormatError(msg)
                return proposal
            return result

    def _command(self, scratch: Path, schema: dict[str, Any]) -> tuple[list[str], Path | None]:
        command = list(self.profile.command)
        if self.profile.kind != "command":
            schema = structured_proposal_schema(schema)
        if self.profile.kind == "codex":
            schema_path = scratch / "proposal.schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            result_path = scratch / "proposal.json"
            command.extend([
                "exec",
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
                "json",
                "--json-schema",
                json.dumps(schema),
            ])
        return command, None

    def _execute(self, command: list[str], payload: bytes, scratch: Path, deadline: float | None) -> bytes:
        invocation_deadline = time.monotonic() + self.profile.timeout_seconds
        if deadline is not None:
            invocation_deadline = min(invocation_deadline, deadline)
        if invocation_deadline <= time.monotonic():
            msg = "managed_decision_timeout"
            raise DriverError(msg)
        self.invocations += 1
        process = subprocess.Popen(  # noqa: S603 - operator-configured argument vector, no shell.
            command,
            cwd=scratch,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            _reap(process)
            msg = "managed process pipes were not created"
            raise DriverError(msg)
        exceeded = Event()
        stdout = _Capture(process.stdout, self.profile.max_output_bytes, exceeded)
        stderr = _Capture(process.stderr, self.profile.max_output_bytes, exceeded)
        threads = [
            Thread(target=stdout.drain, daemon=True),
            Thread(target=stderr.drain, daemon=True),
            Thread(target=_feed, args=(process.stdin, payload), daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            self._wait(process, exceeded, invocation_deadline)
        finally:
            _reap(process)
            for thread in threads:
                thread.join(timeout=1)
        if exceeded.is_set():
            msg = "managed_output_limit"
            raise DriverError(msg)
        return bytes(stdout.contents)

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
