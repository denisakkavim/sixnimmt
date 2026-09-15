"""Run cancellation and setup failures preserve resource ownership."""

import json
import os
import selectors
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from threading import Event, get_ident
from time import monotonic, sleep

import pytest

import sixnimmt.arena.execution as execution
import sixnimmt.persistence.arena as arena_store
from sixnimmt.arena.artifacts import ArenaRunWriter, RunStatus, load_run
from sixnimmt.arena.bots.base import BotOptions, BotSpec, Rejection
from sixnimmt.arena.bots.heuristics import RandomBot
from sixnimmt.arena.bots.lifecycle import BotMatchEnd
from sixnimmt.arena.bots.registry import REGISTRY
from sixnimmt.arena.config import Backend, RunConfig
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.engine.actions import Action
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.engine.views import MatchView
from sixnimmt.persistence.sink import AtomicJsonlWriter
from tests.run_helpers import fixed_plan


class WaitingOptions(BotOptions):
    marker_directory: str


class WaitingBot(RandomBot):
    def __init__(self, seed: int, *, marker_directory: str) -> None:
        super().__init__(seed)
        self.directory = Path(marker_directory)

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        (self.directory / "started").touch()
        deadline = monotonic() + 10
        while not (self.directory / "release").exists():
            if monotonic() >= deadline:
                msg = "test did not release the waiting decision"
                raise TimeoutError(msg)
            sleep(0.01)
        return super().act(view, rejection)

    def cancel(self, reason: str) -> None:
        (self.directory / "cancelled").write_text(reason)
        (self.directory / "release").touch()

    def close(self, result: BotMatchEnd | None) -> None:
        (self.directory / "closed").touch()


@pytest.mark.parametrize("backend", ["thread", "process"])
def test_caller_stop_interrupts_active_decision_and_closes_bot(
    backend: Backend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(REGISTRY, "waiting", BotSpec("waiting", WaitingBot, False, {}, WaitingOptions))
    plan = fixed_plan(
        [PlayerConfig(bot="waiting", options={"marker_directory": str(tmp_path)}), PlayerConfig(bot="random")],
        3,
        66,
        config=RunConfig(backend=backend, match_action_limit=1),
    )
    stopped = Event()
    with ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(execution.run_plan, plan, stop_event=stopped)
        try:
            deadline = monotonic() + 10
            while not (tmp_path / "started").exists():
                assert monotonic() < deadline, "the decision never started"
                sleep(0.01)
            stopped.set()
            result = future.result(timeout=5)
        finally:
            (tmp_path / "release").touch()
    assert result.status.state == "stopped"
    assert result.status.decisions_abandoned == 1
    assert len(result.status.unstarted_job_ids) == 2
    assert len(result.results) == 1
    record = result.results[0]
    assert record.outcome == "abandoned"
    assert record.reason == "operator_stop"
    assert record.actions_accepted == 0
    assert record.seat_decision_calls == (1, 0)
    assert (tmp_path / "cancelled").read_text() == "operator_stop"
    assert (tmp_path / "closed").is_file()


def fail_confirmation(caller_thread: int) -> bool:
    assert get_ident() == caller_thread, "confirmation did not run in the caller thread"
    msg = "confirmation callback failed"
    raise RuntimeError(msg)


def test_confirmation_runs_in_caller_and_closes_waiting_bot_after_callback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(REGISTRY, "waiting", BotSpec("waiting", WaitingBot, False, {}, WaitingOptions))
    plan = fixed_plan(
        [PlayerConfig(bot="waiting", options={"marker_directory": str(tmp_path)}), PlayerConfig(bot="random")],
        1,
        66,
    )
    with pytest.raises(execution.RunExecutionError, match="confirmation callback failed") as captured:
        execution.run_plan(plan, watch=True, confirm_start=partial(fail_confirmation, get_ident()))
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert captured.value.run.status.state == "failed"
    assert (tmp_path / "closed").is_file()
    assert not (tmp_path / "started").exists()


def wait_for_start_prompt(process: subprocess.Popen[bytes]) -> bytes:
    assert process.stderr is not None
    captured = bytearray()
    deadline = monotonic() + 10
    with selectors.DefaultSelector() as selector:
        selector.register(process.stderr, selectors.EVENT_READ)
        while b"Start the match?" not in captured:
            assert monotonic() < deadline, f"the start prompt never appeared: {captured.decode()}"
            if len(selector.select(timeout=0.1)) == 0:
                continue
            chunk = os.read(process.stderr.fileno(), 4096)
            assert chunk != b"", f"the CLI exited before the start prompt: {captured.decode()}"
            captured.extend(chunk)
    return bytes(captured)


@pytest.mark.skipif(os.name != "posix", reason="exercise the terminal SIGINT path")
def test_cli_sigint_at_start_prompt_stops_without_input_and_closes_ready_bot(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    options = tmp_path / "waiting.json"
    options.write_text(json.dumps({"marker_directory": str(tmp_path)}))
    command = [
        sys.executable,
        "-c",
        """
from tests.test_run_ownership import WaitingBot, WaitingOptions
from sixnimmt.arena.bots.base import BotSpec
from sixnimmt.arena.bots.registry import REGISTRY
from sixnimmt.cli import app
REGISTRY["waiting"] = BotSpec("waiting", WaitingBot, False, {}, WaitingOptions)
app()
""",
        "play",
        "--watch",
        "--seat",
        f"waiting:{options}",
        "--seat",
        "random",
        "--output-dir",
        str(directory),
        "--trace",
        "--hands",
        "1",
        "--quiet",
        "--no-animation",
    ]
    process = subprocess.Popen(  # noqa: S603 - fixed local CLI entry point and local test bot.
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        prompt = wait_for_start_prompt(process)
        process.send_signal(signal.SIGINT)
        # Keep stdin open: EOF would unblock a prompt still running in a worker.
        process.wait(timeout=5)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 130, (prompt + stdout + stderr).decode()
        run = load_run(directory)
        assert run.status.state == "stopped"
        assert run.status.error == "operator_stop"
        assert run.results == ()
        assert (tmp_path / "closed").is_file()
        assert not (tmp_path / "started").exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@dataclass
class WriterTracker:
    opened: list[AtomicJsonlWriter] = field(default_factory=list)

    def __call__(self, path: Path) -> AtomicJsonlWriter:
        writer = AtomicJsonlWriter(path)
        self.opened.append(writer)
        return writer


class FinalStatusFailure(ArenaRunWriter):
    def write_status(self, status: RunStatus) -> None:
        if status.state != "running":
            msg = "cannot write final manifest"
            raise OSError(msg)
        super().write_status(status)


class TraceManifestFailure(ArenaRunWriter):
    def _write_trace_manifest(self) -> None:
        (self.directory / "traces").write_text("an obstructing file")
        super()._write_trace_manifest()


def fail_temporary_workspace(*, prefix: str) -> None:
    msg = "cannot create temporary workspace"
    raise OSError(msg)


def test_run_closes_results_after_final_status_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = WriterTracker()
    monkeypatch.setattr(arena_store, "AtomicJsonlWriter", tracker)
    monkeypatch.setattr(execution, "ArenaRunWriter", FinalStatusFailure)
    plan = fixed_plan(
        [PlayerConfig(bot="random")] * 2, 1, 66, protocol=MatchProtocol(end_condition="fixed_hands", hands=1)
    )
    with pytest.raises(OSError, match="final manifest"):
        execution.run_plan(plan, output_dir=tmp_path / "run")
    assert len(tracker.opened) == 1
    assert tracker.opened[0]._handle.closed


def test_run_closes_results_when_temporary_workspace_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = WriterTracker()
    monkeypatch.setattr(arena_store, "AtomicJsonlWriter", tracker)
    monkeypatch.setattr(execution, "TemporaryDirectory", fail_temporary_workspace)
    plan = fixed_plan([PlayerConfig(bot="random")] * 2, 1, 66)
    with pytest.raises(OSError, match="temporary workspace"):
        execution.run_plan(plan, output_dir=tmp_path / "run")
    assert len(tracker.opened) == 1
    assert tracker.opened[0]._handle.closed


def test_writer_closes_results_when_initial_trace_manifest_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = WriterTracker()
    monkeypatch.setattr(arena_store, "AtomicJsonlWriter", tracker)
    monkeypatch.setattr(execution, "ArenaRunWriter", TraceManifestFailure)
    plan = fixed_plan([PlayerConfig(bot="random")] * 2, 1, 66)
    with pytest.raises(FileExistsError):
        execution.run_plan(plan, output_dir=tmp_path / "run", trace=True)
    assert len(tracker.opened) == 1
    assert tracker.opened[0]._handle.closed
