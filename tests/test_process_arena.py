"""Spawned match workers preserve outcomes, provenance, and bounded execution."""

import os
import threading
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import pytest

from sixnimmt.arena.artifacts import load_run
from sixnimmt.arena.bots.base import BotOptions, BotSpec, Rejection
from sixnimmt.arena.bots.heuristics import RandomBot
from sixnimmt.arena.bots.registry import REGISTRY
from sixnimmt.arena.config import Backend, RunConfig
from sixnimmt.arena.match import ArenaError
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.engine.actions import Action, ChooseRowAction
from sixnimmt.engine.replay import replay_events
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.engine.state import Phase
from sixnimmt.engine.views import MatchView
from sixnimmt.persistence.sink import read_action_log, read_event_log
from tests.run_helpers import fixed_plan, match_facts, run_fixed

BASELINES = (
    "random",
    "lowest_card",
    "highest_card",
    "lowest_fitting_card",
    "highest_fitting_card",
    "closest_gap",
    "coldest_row",
    "hand_flexibility",
)


class WorkerOptions(BotOptions):
    marker_directory: str
    tag: str = "custom option"


class WorkerBot(RandomBot):
    def __init__(self, seed: int, *, marker_directory: str, tag: str) -> None:
        super().__init__(seed)
        self.seed = seed
        self.marker_directory = Path(marker_directory)
        self.tag = tag
        self.constructed_pid = os.getpid()
        self.acting_pid: int | None = None
        self.calls = 0
        # Live bot instances need not be picklable: factories run in the worker.
        self.lock = threading.Lock()

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        self.acting_pid = os.getpid()
        if self.calls == 0:
            (self.marker_directory / str(self.acting_pid)).touch()
            deadline = monotonic() + 10
            while len(list(self.marker_directory.iterdir())) < 2:
                if monotonic() >= deadline:
                    msg = "second worker did not start"
                    raise RuntimeError(msg)
                sleep(0.01)
        self.calls += 1
        return super().act(view, rejection)

    def stats(self) -> dict[str, Any]:
        return {
            "constructed_pid": self.constructed_pid,
            "acting_pid": self.acting_pid,
            "seed": self.seed,
            "calls": self.calls,
            "tag": self.tag,
        }


class FaultOptions(BotOptions):
    mode: str


class FaultBot(RandomBot):
    def __init__(self, seed: int, *, mode: str) -> None:
        super().__init__(seed)
        self.mode = mode
        if mode == "late":
            # Let the preceding match's abandoned call finish before this one.
            sleep(0.25)

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if self.mode == "crash":
            os._exit(17)
        if self.mode == "error":
            msg = "bot failed"
            raise RuntimeError(msg)
        if self.mode == "blocked":
            threading.Event().wait(30)
        if self.mode == "late":
            sleep(0.1)
        if self.mode == "illegal":
            return ChooseRowAction(row_index=0)
        return super().act(view, rejection)


def fail_build(seed: int) -> RandomBot:
    msg = "cannot build bot"
    raise RuntimeError(msg)


class FailAtSeed:
    def __init__(self) -> None:
        self.failure_seed: int | None = None

    def __call__(self, seed: int) -> RandomBot:
        if seed == self.failure_seed:
            return fail_build(seed)
        return RandomBot(seed)


@pytest.fixture
def short_protocol() -> MatchProtocol:
    return MatchProtocol(end_condition="fixed_hands", hands=1)


@pytest.fixture
def fault_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(REGISTRY, "fault", BotSpec("fault", FaultBot, False, {}, FaultOptions))


@pytest.mark.parametrize("communication", [False, True])
def test_process_counts_and_traces_match_thread_backend(tmp_path: Path, communication: bool) -> None:
    players = [PlayerConfig(bot=name) for name in BASELINES]
    protocol = MatchProtocol(end_condition="fixed_hands", hands=2, communication_enabled=communication)
    results = []
    all_logs = []
    for backend, concurrency in (("thread", 1), ("process", 1), ("process", 3)):
        directory = tmp_path / f"{backend}-{concurrency}"
        result = run_fixed(
            players,
            4,
            123,
            protocol=protocol,
            output_dir=directory,
            trace=True,
            config=RunConfig(backend=backend, concurrency=concurrency),
        )
        results.append(result)
        assert result.plan.execution.backend == backend
        assert [record.job_id for record in result.results] == [job.job_id for job in result.plan.jobs]
        logs = []
        for record in result.results:
            assert record.event_trace is not None
            assert record.action_trace is not None
            events = read_event_log(directory / record.event_trace)
            logs.append([event.model_dump(exclude={"timestamp"}) for event in events])
            actions = read_action_log(directory / record.action_trace)
            assert len(actions) > 0
            assert all(action.outcome == "accepted" for action in actions)
        all_logs.append(logs)
    assert [match_facts(record) for record in results[0].results] == [
        match_facts(record) for record in results[1].results
    ]
    assert [match_facts(record) for record in results[0].results] == [
        match_facts(record) for record in results[2].results
    ]
    assert all_logs[0] == all_logs[1] == all_logs[2]


def test_custom_factories_build_fresh_bots_in_multiple_processes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, short_protocol: MatchProtocol
) -> None:
    monkeypatch.setitem(REGISTRY, "worker", BotSpec("worker", WorkerBot, True, {}, WorkerOptions))
    markers = tmp_path / "markers"
    markers.mkdir()
    directory = tmp_path / "trace"
    result = run_fixed(
        [PlayerConfig(bot="worker", options={"marker_directory": str(markers)}), PlayerConfig(bot="random")],
        6,
        123,
        protocol=short_protocol,
        output_dir=directory,
        trace=True,
        config=RunConfig(backend="process", concurrency=2),
    )
    assert len(result.results) == 6
    assert all(record.outcome == "finished" for record in result.results)
    worker_pids: set[int] = set()
    for record, job in zip(result.results, result.plan.jobs, strict=True):
        stats = record.seat_stats[0]
        assert stats is not None
        acting_pid = stats["acting_pid"]
        assert isinstance(acting_pid, int)
        assert stats["constructed_pid"] == acting_pid != os.getpid()
        worker_pids.add(acting_pid)
        assert stats["seed"] == job.seats[0].bot_seed
        assert record.action_trace is not None
        actions = read_action_log(directory / record.action_trace)
        assert stats["calls"] == sum(action.player_id == "player_1" for action in actions)
        assert stats["tag"] == "custom option"
    assert len(worker_pids) == 2


def test_process_backend_rejects_local_factory_before_creating_traces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(REGISTRY, "local", BotSpec("local", lambda seed: RandomBot(seed), True, {}))
    directory = tmp_path / "trace"
    with pytest.raises(ValueError, match="importable module-level factory"):
        run_fixed(
            [PlayerConfig(bot="local"), PlayerConfig(bot="random")],
            2,
            123,
            output_dir=directory,
            trace=True,
            config=RunConfig(backend="process"),
        )
    assert not directory.exists()


def test_process_backend_rejects_observer_before_creating_traces(tmp_path: Path) -> None:
    directory = tmp_path / "trace"
    with pytest.raises(ValueError, match="observer callbacks"):
        run_fixed(
            [PlayerConfig(bot="random")] * 2,
            2,
            123,
            output_dir=directory,
            trace=True,
            config=RunConfig(backend="process"),
            observer=lambda state, events: None,
        )
    assert not directory.exists()


def test_process_construction_failures_are_saved_as_match_outcomes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(REGISTRY, "broken", BotSpec("broken", fail_build, True, {}))
    directory = tmp_path / "trace"
    result = run_fixed(
        [PlayerConfig(bot="broken"), PlayerConfig(bot="random")],
        3,
        123,
        output_dir=directory,
        trace=True,
        config=RunConfig(backend="process"),
    )
    assert result.status.state == "completed"
    assert len(result.results) == 3
    assert all(
        record.outcome == "failed" and record.scores is None and record.ended_by == 0 for record in result.results
    )
    assert all(record.reason is not None and "cannot build bot" in record.reason for record in result.results)
    assert load_run(directory).results == result.results


def test_later_process_build_failure_is_a_match_outcome(
    monkeypatch: pytest.MonkeyPatch, short_protocol: MatchProtocol
) -> None:
    factory = FailAtSeed()
    monkeypatch.setitem(REGISTRY, "transient", BotSpec("transient", factory, True, {}))
    plan = fixed_plan([PlayerConfig(bot="transient"), PlayerConfig(bot="random")], 3, 123, protocol=short_protocol)
    factory.failure_seed = plan.jobs[1].seats[0].bot_seed
    result = run_fixed(
        [PlayerConfig(bot="transient"), PlayerConfig(bot="random")],
        3,
        123,
        protocol=short_protocol,
        config=RunConfig(backend="process", concurrency=2),
    )
    assert [record.outcome for record in result.results] == ["finished", "failed", "finished"]
    assert result.results[1].scores is None


@pytest.mark.parametrize(("mode", "outcome"), [("error", "failed"), ("illegal", "forfeited")])
def test_process_failures_do_not_contribute_scores(fault_registry: None, mode: str, outcome: str) -> None:
    result = run_fixed(
        [PlayerConfig(bot="fault", options={"mode": mode}), PlayerConfig(bot="random")],
        3,
        123,
        config=RunConfig(backend="process", concurrency=2),
    )
    assert len(result.results) == 3
    assert all(
        record.outcome == outcome and record.scores is None and record.winners == () for record in result.results
    )


def test_process_stop_on_failure_drains_submitted_matches(fault_registry: None) -> None:
    result = run_fixed(
        [PlayerConfig(bot="fault", options={"mode": "error"}), PlayerConfig(bot="random")],
        20,
        123,
        config=RunConfig(backend="process", concurrency=2, stop_on_failure=True),
    )
    assert len(result.status.started_job_ids) == len(result.status.completed_job_ids) == len(result.results) == 2
    assert all(record.outcome == "failed" for record in result.results)
    assert result.status.state == "stopped"


def test_crashed_process_reports_run_error_and_writes_manifest(fault_registry: None, tmp_path: Path) -> None:
    directory = tmp_path / "trace"
    with pytest.raises(ArenaError, match="arena execution failed"):
        run_fixed(
            [PlayerConfig(bot="fault", options={"mode": "crash"}), PlayerConfig(bot="random")],
            20,
            123,
            output_dir=directory,
            trace=True,
            config=RunConfig(backend="process", concurrency=2),
        )
    status = load_run(directory).status
    assert len(status.started_job_ids) == len(status.lost_job_ids) == 2
    assert status.completed_job_ids == ()
    assert status.error is not None and "BrokenProcessPool" in status.error


def test_process_timeouts_are_counted_across_workers(fault_registry: None, tmp_path: Path) -> None:
    directory = tmp_path / "trace"
    result = run_fixed(
        [PlayerConfig(bot="fault", options={"mode": "blocked"}), PlayerConfig(bot="random")],
        4,
        123,
        output_dir=directory,
        trace=True,
        config=RunConfig(backend="process", concurrency=2, decision_timeout_seconds=0.05),
    )
    assert len(result.results) == result.status.decisions_abandoned == 4
    assert all(record.outcome == "failed" and record.reason == "decision_timeout" for record in result.results)
    assert load_run(directory).status.decisions_abandoned == 4


def test_process_abandoned_bound_stops_new_matches(fault_registry: None, tmp_path: Path) -> None:
    directory = tmp_path / "trace"
    with pytest.raises(ArenaError, match="max_abandoned_decisions"):
        run_fixed(
            [PlayerConfig(bot="fault", options={"mode": "blocked"}), PlayerConfig(bot="random")],
            20,
            123,
            output_dir=directory,
            trace=True,
            config=RunConfig(
                backend="process", concurrency=2, decision_timeout_seconds=0.05, max_abandoned_decisions=0
            ),
        )
    status = load_run(directory).status
    assert len(status.started_job_ids) == len(status.completed_job_ids) == 2
    assert status.decisions_abandoned == 2


def test_late_process_decisions_release_active_slots(fault_registry: None) -> None:
    result = run_fixed(
        [PlayerConfig(bot="fault", options={"mode": "late"}), PlayerConfig(bot="random")],
        3,
        123,
        config=RunConfig(backend="process", concurrency=1, decision_timeout_seconds=0.02, max_abandoned_decisions=1),
    )
    assert len(result.results) == result.status.decisions_abandoned == 3
    assert all(record.outcome == "failed" for record in result.results)


@pytest.mark.arena_slow
@pytest.mark.parametrize("communication", [False, True])
def test_process_volume_matches_thread_results_and_replays(tmp_path: Path, communication: bool) -> None:
    players = [PlayerConfig(bot=name) for name in BASELINES]
    protocol = MatchProtocol(end_condition="fixed_hands", hands=3, communication_enabled=communication)
    sequential = run_fixed(players, 100, 1234, protocol=protocol)
    directory = tmp_path / "trace"
    parallel = run_fixed(
        players,
        100,
        1234,
        protocol=protocol,
        output_dir=directory,
        trace=True,
        config=RunConfig(backend="process", concurrency=4),
    )
    assert [match_facts(record) for record in parallel.results] == [
        match_facts(record) for record in sequential.results
    ]
    assert len(parallel.results) == 100
    assert all(record.outcome == "finished" for record in parallel.results)
    replay_scores = [0] * len(players)
    recorded_scores = [0] * len(players)
    for record in parallel.results:
        assert record.event_trace is not None
        assert record.scores is not None
        for index, score in enumerate(record.scores):
            recorded_scores[index] += score
        events = read_event_log(directory / record.event_trace)
        replay = replay_events(events)
        assert replay.state.phase == Phase.FINISHED
        assert replay.state.hand_number == 3
        final_event = events[-1]
        assert final_event.type == "match_ended"
        assert tuple(final_event.data["winners"]) == tuple(f"player_{index + 1}" for index in record.winners)
        for index, player in enumerate(replay.state.players):
            replay_scores[index] += player.total_score
    assert replay_scores == recorded_scores


@pytest.mark.parametrize("backend", ["thread", "process"])
@pytest.mark.parametrize("action_limit", [1, 10_000])
def test_progress_counts_each_collected_outcome(backend: Backend, action_limit: int) -> None:
    counts: list[int] = []
    result = run_fixed(
        [PlayerConfig(bot="random"), PlayerConfig(bot="lowest_card")],
        4,
        66,
        config=RunConfig(backend=backend, concurrency=2, match_action_limit=action_limit),
        on_progress=counts.append,
    )
    assert counts == [1, 2, 3, 4]
    assert counts[-1] == len(result.results)
    assert sum(record.outcome == "abandoned" for record in result.results) == (4 if action_limit == 1 else 0)
