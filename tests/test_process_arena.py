"""Spawned match workers preserve outcomes, provenance, and bounded execution."""

import json
import os
import threading
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import pytest

from sixnimmt.arena.bots import REGISTRY, BotOptions, BotSpec, RandomBot, Rejection
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.arena.runner import ArenaError, derive_seed, run_arena
from sixnimmt.engine.actions import Action, ChooseRowAction
from sixnimmt.engine.replay import replay_events
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.engine.state import Phase
from sixnimmt.engine.views import MatchView
from sixnimmt.persistence.sink import read_action_log, read_event_log

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


def fail_later_build(seed: int) -> RandomBot:
    if seed == derive_seed(123, "bot", 1, 0):
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
        result = run_arena(
            players,
            4,
            123,
            protocol=protocol,
            config=RunConfig(backend=backend, concurrency=concurrency, trace_dir=directory),
        )
        results.append(result)
        manifest = json.loads((directory / "manifest.json").read_text())
        assert manifest["run_config"]["backend"] == backend
        assert [entry["game_index"] for entry in manifest["matches"]] == list(range(4))
        logs = []
        for entry in manifest["matches"]:
            events = read_event_log(directory / entry["log"])
            logs.append([event.model_dump(exclude={"timestamp"}) for event in events])
            actions = read_action_log(directory / entry["actions"])
            assert len(actions) > 0
            assert all(action.outcome == "accepted" for action in actions)
        all_logs.append(logs)
    assert results[0] == results[1] == results[2]
    assert all_logs[0] == all_logs[1] == all_logs[2]


def test_custom_factories_build_fresh_bots_in_multiple_processes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, short_protocol: MatchProtocol
) -> None:
    monkeypatch.setitem(REGISTRY, "worker", BotSpec("worker", WorkerBot, True, {}, WorkerOptions))
    markers = tmp_path / "markers"
    markers.mkdir()
    directory = tmp_path / "trace"
    result = run_arena(
        [PlayerConfig(bot="worker", options={"marker_directory": str(markers)}), PlayerConfig(bot="random")],
        6,
        123,
        protocol=short_protocol,
        config=RunConfig(backend="process", concurrency=2, trace_dir=directory),
    )
    assert result.finished == 6
    manifest = json.loads((directory / "manifest.json").read_text())
    worker_pids = set()
    for entry in manifest["matches"]:
        stats = entry["seat_stats"]["player_1"]
        assert stats["constructed_pid"] == stats["acting_pid"] != os.getpid()
        worker_pids.add(stats["acting_pid"])
        assert stats["seed"] == derive_seed(123, "bot", entry["game_index"], 0)
        actions = read_action_log(directory / entry["actions"])
        assert stats["calls"] == sum(action.player_id == "player_1" for action in actions)
        assert stats["tag"] == "custom option"
    assert len(worker_pids) == 2


def test_process_backend_rejects_local_factory_before_creating_traces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(REGISTRY, "local", BotSpec("local", lambda seed: RandomBot(seed), True, {}))
    directory = tmp_path / "trace"
    with pytest.raises(ValueError, match="importable module-level factory"):
        run_arena(
            [PlayerConfig(bot="local"), PlayerConfig(bot="random")],
            2,
            123,
            config=RunConfig(backend="process", trace_dir=directory),
        )
    assert not directory.exists()


def test_process_backend_rejects_observer_before_creating_traces(tmp_path: Path) -> None:
    directory = tmp_path / "trace"
    with pytest.raises(ValueError, match="observer callbacks"):
        run_arena(
            [PlayerConfig(bot="random")] * 2,
            2,
            123,
            config=RunConfig(backend="process", trace_dir=directory),
            observer=lambda state, events: None,
        )
    assert not directory.exists()


def test_first_process_build_failure_is_a_run_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(REGISTRY, "broken", BotSpec("broken", fail_build, True, {}))
    directory = tmp_path / "trace"
    with pytest.raises(ArenaError, match="first game's lineup"):
        run_arena(
            [PlayerConfig(bot="broken"), PlayerConfig(bot="random")],
            3,
            123,
            config=RunConfig(backend="process", trace_dir=directory),
        )
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["games_started"] == 1
    assert manifest["games_completed"] == 0
    assert "cannot build bot" in manifest["error"]


def test_later_process_build_failure_is_a_match_outcome(
    monkeypatch: pytest.MonkeyPatch, short_protocol: MatchProtocol
) -> None:
    monkeypatch.setitem(REGISTRY, "transient", BotSpec("transient", fail_later_build, True, {}))
    result = run_arena(
        [PlayerConfig(bot="transient"), PlayerConfig(bot="random")],
        3,
        123,
        protocol=short_protocol,
        config=RunConfig(backend="process", concurrency=2),
    )
    assert result.finished == 2
    assert result.failed == 1


@pytest.mark.parametrize(("mode", "outcome"), [("error", "failed"), ("illegal", "forfeited")])
def test_process_failures_do_not_contribute_scores(fault_registry: None, mode: str, outcome: str) -> None:
    result = run_arena(
        [PlayerConfig(bot="fault", options={"mode": mode}), PlayerConfig(bot="random")],
        3,
        123,
        config=RunConfig(backend="process", concurrency=2),
    )
    assert getattr(result, outcome) == 3
    assert all(player.total_score == player.wins == player.ties == 0 for player in result.players)


def test_process_stop_on_failure_drains_submitted_matches(fault_registry: None) -> None:
    result = run_arena(
        [PlayerConfig(bot="fault", options={"mode": "error"}), PlayerConfig(bot="random")],
        20,
        123,
        config=RunConfig(backend="process", concurrency=2, stop_on_failure=True),
    )
    assert result.games_started == result.games_completed == result.failed == 2


def test_crashed_process_reports_run_error_and_writes_manifest(fault_registry: None, tmp_path: Path) -> None:
    directory = tmp_path / "trace"
    with pytest.raises(ArenaError, match="arena run failed"):
        run_arena(
            [PlayerConfig(bot="fault", options={"mode": "crash"}), PlayerConfig(bot="random")],
            20,
            123,
            config=RunConfig(backend="process", concurrency=2, trace_dir=directory),
        )
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["games_started"] == 2
    assert manifest["games_completed"] == 0
    assert "BrokenProcessPool" in manifest["error"]


def test_process_timeouts_are_counted_across_workers(fault_registry: None, tmp_path: Path) -> None:
    directory = tmp_path / "trace"
    result = run_arena(
        [PlayerConfig(bot="fault", options={"mode": "blocked"}), PlayerConfig(bot="random")],
        4,
        123,
        config=RunConfig(backend="process", concurrency=2, decision_timeout_seconds=0.05, trace_dir=directory),
    )
    assert result.failed == result.decisions_abandoned == 4
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["decisions_abandoned"] == 4
    assert all(entry["reason"] == "decision_timeout" for entry in manifest["matches"])


def test_process_abandoned_bound_stops_new_matches(fault_registry: None, tmp_path: Path) -> None:
    directory = tmp_path / "trace"
    with pytest.raises(ArenaError, match="max_abandoned_decisions"):
        run_arena(
            [PlayerConfig(bot="fault", options={"mode": "blocked"}), PlayerConfig(bot="random")],
            20,
            123,
            config=RunConfig(
                backend="process",
                concurrency=2,
                decision_timeout_seconds=0.05,
                max_abandoned_decisions=0,
                trace_dir=directory,
            ),
        )
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["games_started"] == manifest["games_completed"] == 2
    assert manifest["decisions_abandoned"] == 2


def test_late_process_decisions_release_active_slots(fault_registry: None) -> None:
    result = run_arena(
        [PlayerConfig(bot="fault", options={"mode": "late"}), PlayerConfig(bot="random")],
        3,
        123,
        config=RunConfig(backend="process", concurrency=1, decision_timeout_seconds=0.02, max_abandoned_decisions=1),
    )
    assert result.failed == result.decisions_abandoned == 3


@pytest.mark.arena_slow
@pytest.mark.parametrize("communication", [False, True])
def test_process_volume_matches_thread_results_and_replays(tmp_path: Path, communication: bool) -> None:
    players = [PlayerConfig(bot=name) for name in BASELINES]
    protocol = MatchProtocol(end_condition="fixed_hands", hands=3, communication_enabled=communication)
    sequential = run_arena(players, 100, 1234, protocol=protocol)
    directory = tmp_path / "trace"
    parallel = run_arena(
        players,
        100,
        1234,
        protocol=protocol,
        config=RunConfig(backend="process", concurrency=4, trace_dir=directory),
    )
    assert parallel == sequential
    assert parallel.finished == 100
    manifest = json.loads((directory / "manifest.json").read_text())
    assert len(manifest["matches"]) == 100
    replay_scores = [0] * len(players)
    for entry in manifest["matches"]:
        events = read_event_log(directory / entry["log"])
        replay = replay_events(events)
        assert replay.state.phase == Phase.FINISHED
        assert replay.state.hand_number == 3
        assert tuple(events[-1].data["winners"]) == tuple(entry["winners"])
        for index, player in enumerate(replay.state.players):
            replay_scores[index] += player.total_score
    assert replay_scores == [player.total_score for player in parallel.players]
