"""Changing lineups preserve deterministic facts and durable missing-job coverage."""

import json
import os
from dataclasses import replace
from pathlib import Path
from time import sleep
from typing import Any

import pytest

from sixnimmt.analytics.evaluation import analyse_run
from sixnimmt.analytics.summary import summarise
from sixnimmt.arena.bots.base import ActionBatch, BotOptions, BotSpec, Rejection
from sixnimmt.arena.bots.heuristics import RandomBot
from sixnimmt.arena.bots.registry import REGISTRY
from sixnimmt.arena.catalogue import CandidateConfig
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.planned import PlannedArenaError, load_run, run_plan
from sixnimmt.arena.planning import ArenaPlan, LineupConfig, build_arena_plan
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.arena.records import MatchRecord
from sixnimmt.arena.runner import run_arena
from sixnimmt.engine.actions import Action, ChooseRowAction
from sixnimmt.engine.events import Event
from sixnimmt.engine.replay import replay_events
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.engine.state import MatchState
from sixnimmt.engine.views import MatchView
from sixnimmt.persistence.manifest import ManifestMatch
from sixnimmt.persistence.sink import read_action_log, read_event_log


class AuditOptions(BotOptions):
    output_directory: str
    fail_after_hand: int | None = None
    crash: bool = False
    delay_seconds: float = 0


class AuditBot(RandomBot):
    def __init__(
        self, seed: int, *, output_directory: str, fail_after_hand: int | None, crash: bool, delay_seconds: float
    ) -> None:
        directory = Path(output_directory)
        assert (directory / "plan.json").is_file()
        assert (directory / "manifest.json").is_file()
        plan = json.loads((directory / "plan.json").read_text())
        assert len(plan["jobs"]) > 0
        assert isinstance(plan["analysis"], dict)
        super().__init__(seed)
        self.seed = seed
        self.fail_after_hand = fail_after_hand
        self.crash = crash
        self.delay_seconds = delay_seconds
        self.calls = 0
        self.pid = os.getpid()

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if self.crash:
            os._exit(17)
        if self.delay_seconds > 0:
            sleep(self.delay_seconds)
        if self.fail_after_hand is not None and view.hand_number > self.fail_after_hand:
            msg = "configured decision failure"
            raise RuntimeError(msg)
        self.calls += 1
        return super().act(view, rejection)

    def stats(self) -> dict[str, Any]:
        return {"seed": self.seed, "calls": self.calls, "pid": self.pid}


class OversizedBatchBot:
    def __init__(self, seed: int) -> None:
        pass

    def act(self, view: MatchView, rejection: Rejection | None = None) -> ActionBatch:
        return ActionBatch((ChooseRowAction(row_index=0), ChooseRowAction(row_index=0)))


@pytest.fixture
def short_plan() -> ArenaPlan:
    return build_arena_plan(
        LineupConfig(
            catalogue=(CandidateConfig(bot="random"), CandidateConfig(bot="closest_gap")),
            player_counts=(4, 5),
            games=2,
            rotations=False,
            protocol=MatchProtocol(end_condition="fixed_hands", hands=1, anonymise_display_names=True),
        )
    )


@pytest.fixture
def audit_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(REGISTRY, "audit", BotSpec("audit", AuditBot, True, {"strategy": "secret"}, AuditOptions))


def _facts(record: MatchRecord) -> dict[str, Any]:
    return record.model_dump(
        exclude={"duration_seconds", "seat_decision_seconds", "seat_decision_samples", "event_trace", "action_trace"}
    )


def _reject_artifact_writer(*args: object, **kwargs: object) -> None:
    msg = "an in-memory run must not construct an artifact writer"
    raise AssertionError(msg)


@pytest.mark.parametrize("backend", ["thread", "process"])
def test_omitted_output_directory_keeps_all_results_in_memory(
    short_plan: ArenaPlan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    monkeypatch.chdir(tmp_path)
    config = RunConfig(backend=backend, concurrency=2)
    with monkeypatch.context() as guard:
        guard.setattr("sixnimmt.arena.planned.ArenaRunWriter", _reject_artifact_writer)
        memory = run_plan(short_plan, config=config)
    assert memory.artifact_dir is None
    assert list(tmp_path.iterdir()) == []
    assert memory.status.state == "completed"
    assert memory.provenance["trace_enabled"] is False
    assert all(result.event_trace is None and result.action_trace is None for result in memory.results)
    saved = run_plan(short_plan, config=config, output_dir=tmp_path / "saved")
    assert memory.plan == saved.plan
    assert memory.status == saved.status
    assert [_facts(result) for result in memory.results] == [_facts(result) for result in saved.results]
    assert analyse_run(memory).artifact_dir is None


def test_traces_require_an_explicit_output_directory(
    short_plan: ArenaPlan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="requires an output_dir"):
        run_plan(short_plan, trace=True)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("communication", [False, True])
def test_variable_player_counts_match_across_backends(tmp_path: Path, communication: bool) -> None:
    counts = (2, 3, 4, 5, 6, 10)
    plan = build_arena_plan(
        LineupConfig(
            catalogue=(CandidateConfig(bot="random"), CandidateConfig(bot="closest_gap")),
            player_counts=counts,
            games=1,
            protocol=MatchProtocol(
                end_condition="fixed_hands", hands=1, communication_enabled=communication, anonymise_display_names=True
            ),
        )
    )
    records = []
    for backend, concurrency in (("thread", 1), ("thread", 3), ("process", 2)):
        run = run_plan(
            plan,
            config=RunConfig(backend=backend, concurrency=concurrency),
            output_dir=tmp_path / f"{backend}-{concurrency}",
        )
        assert run.status.state == "completed"
        assert {len(result.seats) for result in run.results} == set(counts)
        records.append([_facts(result) for result in run.results])
    assert records[0] == records[1] == records[2]


def test_saved_run_reloads_without_registered_factories(
    short_plan: ArenaPlan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = run_plan(short_plan, output_dir=tmp_path / "run")
    assert run.artifact_dir is not None
    monkeypatch.delitem(REGISTRY, "random")
    monkeypatch.delitem(REGISTRY, "closest_gap")
    assert load_run(run.artifact_dir) == run
    manifest = json.loads((run.artifact_dir / "manifest.json").read_text())
    assert manifest["artifact_kind"] == "arena_run"
    assert manifest["status"]["state"] == "completed"
    assert manifest["provenance"]["file_sha256"]["uv.lock"] is not None
    assert manifest["provenance"]["dependencies"]["sixnimmt"] == "0.0.1"


def test_run_keeps_one_copy_of_plan_status_and_provenance(short_plan: ArenaPlan, tmp_path: Path) -> None:
    run = run_plan(short_plan, output_dir=tmp_path / "run")
    assert run.artifact_dir is not None
    assert {path.name for path in run.artifact_dir.iterdir()} == {"plan.json", "results.jsonl", "manifest.json"}
    plan = json.loads((run.artifact_dir / "plan.json").read_text())
    manifest = json.loads((run.artifact_dir / "manifest.json").read_text())
    assert len(plan["jobs"]) == len(run.plan.jobs)
    assert plan["analysis"] == run.plan.analysis.model_dump(mode="json")
    assert manifest["status"] == run.status.model_dump(mode="json")
    assert manifest["provenance"] == run.provenance
    assert manifest["files"]["analysis"] == "analysis.json.gz"
    assert load_run(run.artifact_dir) == run


def test_legacy_duplicate_files_remain_readable(short_plan: ArenaPlan, tmp_path: Path) -> None:
    run = run_plan(short_plan, output_dir=tmp_path / "run")
    assert run.artifact_dir is not None
    directory = run.artifact_dir
    (directory / "jobs.jsonl").write_text("\n".join(job.model_dump_json() for job in run.plan.jobs) + "\n")
    (directory / "status.json").write_text(run.status.model_dump_json())
    (directory / "provenance.json").write_text(json.dumps(run.provenance))
    (directory / "analysis-spec.json").write_text(run.plan.analysis.model_dump_json())
    (directory / "results.jsonl").write_text("\n".join(result.model_dump_json() for result in run.results) + "\n")
    assert load_run(directory) == run


@pytest.mark.parametrize("backend", ["thread", "process"])
def test_untraced_run_keeps_timing_totals_without_individual_samples(
    short_plan: ArenaPlan, tmp_path: Path, backend: str
) -> None:
    run = run_plan(short_plan, output_dir=tmp_path / "run", config=RunConfig(backend=backend))
    assert run.artifact_dir is not None
    assert all(record.seat_decision_samples == () for record in run.results)
    assert "seat_decision_samples" not in (run.artifact_dir / "results.jsonl").read_text()
    loaded = load_run(run.artifact_dir)
    assert loaded == run
    measured = analyse_run(loaded).diagnostics
    assert measured.measured_decision_calls == sum(sum(record.seat_decision_calls) for record in run.results)
    assert measured.measured_decision_calls > 0
    assert measured.decision_seconds_total is not None
    assert measured.decision_seconds_total > 0
    assert measured.decision_seconds_median is None
    assert measured.decision_seconds_p95 is None


def test_mismatched_legacy_jobs_are_rejected(short_plan: ArenaPlan, tmp_path: Path) -> None:
    run = run_plan(short_plan, output_dir=tmp_path / "run")
    assert run.artifact_dir is not None
    (run.artifact_dir / "jobs.jsonl").write_text("")
    with pytest.raises(ValueError, match="planned-job records"):
        load_run(run.artifact_dir)


@pytest.mark.parametrize("backend", ["thread", "process"])
def test_fresh_bots_receive_explicit_seeds_after_plan_is_durable(
    audit_registry: None, tmp_path: Path, backend: str
) -> None:
    directory = tmp_path / "run"
    plan = build_arena_plan(
        LineupConfig(
            catalogue=(CandidateConfig(bot="audit", label="secret", options={"output_directory": str(directory)}),),
            games=2,
            rotations=False,
            protocol=MatchProtocol(end_condition="fixed_hands", hands=1, anonymise_display_names=True),
        )
    )
    run = run_plan(plan, config=RunConfig(backend=backend, concurrency=2), output_dir=directory)
    for record in run.results:
        for index, (assignment, stats) in enumerate(zip(record.seats, record.seat_stats, strict=True)):
            assert stats is not None
            assert stats["seed"] == assignment.bot_seed
            assert stats["calls"] == record.seat_decision_calls[index]
            assert (stats["pid"] == os.getpid()) == (backend == "thread")


def _check_anonymous_seats(state: MatchState, events: tuple[Event, ...]) -> None:
    for index, player in enumerate(state.players):
        assert player.agent_metadata == {}
        assert player.display_name == f"Player {index + 1}"


def test_analysis_labels_are_absent_from_engine_state(audit_registry: None, tmp_path: Path) -> None:
    directory = tmp_path / "run"
    plan = build_arena_plan(
        LineupConfig(
            catalogue=(
                CandidateConfig(
                    bot="audit", label="secret", family="secret", options={"output_directory": str(directory)}
                ),
            ),
            games=1,
            rotations=False,
            protocol=MatchProtocol(end_condition="fixed_hands", hands=1, anonymise_display_names=True),
        )
    )
    run = run_plan(plan, output_dir=directory, observer=_check_anonymous_seats)
    assert all(record.outcome == "finished" for record in run.results)


def test_partial_current_hand_is_distinct_from_completed_hands(short_plan: ArenaPlan, tmp_path: Path) -> None:
    plan = short_plan.model_copy(update={"protocol": short_plan.protocol.model_copy(update={"hands": 2})})
    run = run_plan(plan, config=RunConfig(match_action_limit=70), output_dir=tmp_path / "run")
    for record in run.results:
        assert record.outcome == "abandoned"
        assert record.scores is None
        assert record.winners == ()
        assert record.completed_hands == 1
        assert len(record.completed_hand_scores[0]) == len(record.seats)
        assert record.partial_scores is not None
        assert record.reason == "match_action_limit"


def test_traces_agree_with_compact_scores_and_actual_call_measurements(short_plan: ArenaPlan, tmp_path: Path) -> None:
    run = run_plan(
        short_plan,
        output_dir=tmp_path / "run",
        trace=True,
    )
    assert run.artifact_dir is not None
    manifest = json.loads((run.artifact_dir / "traces" / "manifest.json").read_text())
    entries = {entry["match_id"]: ManifestMatch.model_validate(entry) for entry in manifest["matches"]}
    assert run.plan.execution.trace_dir is None
    assert run.provenance["trace_enabled"] is True
    for record in run.results:
        assert record.event_trace is not None
        assert record.action_trace is not None
        events = read_event_log(run.artifact_dir / record.event_trace)
        actions = read_action_log(run.artifact_dir / record.action_trace)
        summary = summarise(events, actions, entries[record.match_id])
        assert summary.has_manifest
        assert summary.outcome == record.outcome.value
        replayed = replay_events(events)
        assert record.scores == tuple(player.total_score for player in replayed.state.players)
        assert record.completed_hand_scores == (record.scores,)
        for index, samples in enumerate(record.seat_decision_samples):
            measured = tuple(
                action.decision_duration_ms / 1000
                for action in actions
                if action.player_id == f"player_{index + 1}" and action.decision_duration_ms is not None
            )
            assert samples == measured
            assert record.seat_decision_seconds[index] == sum(measured)
        assert record.seat_stats == (None,) * len(record.seats)


def test_external_trace_directory_is_rejected_before_creating_artifacts(short_plan: ArenaPlan, tmp_path: Path) -> None:
    directory = tmp_path / "run"
    with pytest.raises(ValueError, match="output_dir"):
        run_plan(short_plan, output_dir=directory, config=RunConfig(trace_dir=tmp_path / "outside"))
    assert not directory.exists()
    assert not (tmp_path / "outside").exists()


def test_saved_traced_plan_can_run_in_another_output_directory(short_plan: ArenaPlan, tmp_path: Path) -> None:
    initial = run_plan(short_plan, output_dir=tmp_path / "first", trace=True)
    assert initial.artifact_dir is not None
    loaded = load_run(initial.artifact_dir)
    replayed = run_plan(loaded.plan, output_dir=tmp_path / "second")
    assert replayed.artifact_dir is not None
    assert [_facts(record) for record in initial.results] == [_facts(record) for record in replayed.results]
    assert replayed.provenance["trace_enabled"] is False
    assert not (replayed.artifact_dir / "traces").exists()


@pytest.mark.parametrize("trace", [False, True])
def test_call_is_measured_when_batch_exceeds_action_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, trace: bool
) -> None:
    monkeypatch.setitem(REGISTRY, "batch", BotSpec("batch", OversizedBatchBot, True, {}))
    plan = build_arena_plan(
        LineupConfig(
            catalogue=(CandidateConfig(bot="batch"),),
            player_counts=(4,),
            games=1,
            rotations=False,
        )
    )
    run = run_plan(plan, output_dir=tmp_path / "run", config=RunConfig(match_action_limit=1), trace=trace)
    record = run.results[0]
    assert record.reason == "match_action_limit"
    assert record.actions_accepted == record.actions_rejected == 0
    assert record.seat_decision_calls == (1, 0, 0, 0)
    if trace:
        assert len(record.seat_decision_samples[0]) == 1
    else:
        assert record.seat_decision_samples == ()
    assert record.seat_decision_seconds[0] is not None


@pytest.mark.parametrize("backend", ["thread", "process"])
def test_stop_on_failure_persists_every_submitted_outcome_and_unstarted_job(
    audit_registry: None, tmp_path: Path, backend: str
) -> None:
    directory = tmp_path / "run"
    plan = build_arena_plan(
        LineupConfig(
            catalogue=(
                CandidateConfig(bot="audit", options={"output_directory": str(directory), "fail_after_hand": 0}),
            ),
            player_counts=(2, 10),
            games=5,
            rotations=False,
        )
    )
    run = run_plan(plan, output_dir=directory, config=RunConfig(backend=backend, concurrency=2, stop_on_failure=True))
    assert run.status.state == "stopped"
    assert len(run.status.started_job_ids) == len(run.results) == 2
    assert len(run.status.unstarted_job_ids) == 8
    assert run.status.lost_job_ids == ()
    assert all(result.outcome == "failed" for result in run.results)
    assert load_run(directory) == run


def test_crashed_process_preserves_lost_and_unstarted_jobs(audit_registry: None, tmp_path: Path) -> None:
    directory = tmp_path / "run"
    plan = build_arena_plan(
        LineupConfig(
            catalogue=(CandidateConfig(bot="audit", options={"output_directory": str(directory), "crash": True}),),
            player_counts=(2, 10),
            games=5,
            rotations=False,
        )
    )
    with pytest.raises(PlannedArenaError, match="saved run") as captured:
        run_plan(plan, output_dir=directory, config=RunConfig(backend="process", concurrency=2))
    run = load_run(directory)
    assert run == captured.value.run
    assert run.status.state == "failed"
    assert len(run.status.lost_job_ids) == 2
    assert len(run.status.unstarted_job_ids) == 8
    assert run.results == ()


@pytest.mark.parametrize("backend", ["thread", "process"])
def test_timeout_limit_retains_returned_failures_and_unavailable_resources(
    audit_registry: None, tmp_path: Path, backend: str
) -> None:
    directory = tmp_path / "run"
    plan = build_arena_plan(
        LineupConfig(
            catalogue=(
                CandidateConfig(bot="audit", options={"output_directory": str(directory), "delay_seconds": 0.5}),
            ),
            player_counts=(2, 10),
            games=5,
            rotations=False,
        )
    )
    with pytest.raises(PlannedArenaError, match="max_abandoned_decisions"):
        run_plan(
            plan,
            output_dir=directory,
            config=RunConfig(backend=backend, concurrency=2, decision_timeout_seconds=0.01, max_abandoned_decisions=0),
        )
    run = load_run(directory)
    assert run.status.decisions_abandoned == len(run.results) == 2
    assert run.status.lost_job_ids == ()
    assert len(run.status.unstarted_job_ids) == 8
    for record in run.results:
        assert record.reason == "decision_timeout"
        assert record.scores is None
        assert record.seat_stats[0] is None
        assert record.seat_decision_calls[0] == 1
        assert record.seat_decision_seconds[1] is None


def _fail_progress(count: int) -> None:
    msg = "progress callback failed"
    raise RuntimeError(msg)


def test_progress_callback_failure_drains_already_submitted_jobs(short_plan: ArenaPlan, tmp_path: Path) -> None:
    directory = tmp_path / "run"
    with pytest.raises(PlannedArenaError, match="progress callback failed"):
        run_plan(short_plan, output_dir=directory, config=RunConfig(concurrency=2), on_progress=_fail_progress)
    run = load_run(directory)
    assert len(run.results) == 2
    assert len(run.status.unstarted_job_ids) == 2
    assert run.status.lost_job_ids == ()


def test_in_memory_failure_retains_collected_results_without_a_saved_path(
    short_plan: ArenaPlan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PlannedArenaError, match="progress callback failed") as captured:
        run_plan(short_plan, config=RunConfig(concurrency=2), on_progress=_fail_progress)
    run = captured.value.run
    assert run.artifact_dir is None
    assert len(run.results) == 2
    assert run.status.state == "failed"
    assert "saved run" not in str(captured.value)
    assert list(tmp_path.iterdir()) == []


def test_fixed_lineup_progress_callback_error_propagates_directly() -> None:
    with pytest.raises(RuntimeError, match=r"^progress callback failed$") as captured:
        run_arena(
            [PlayerConfig(bot="random")] * 4,
            2,
            66,
            protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
            on_progress=_fail_progress,
        )
    assert type(captured.value) is RuntimeError


def test_durable_results_recover_from_stale_status(short_plan: ArenaPlan, tmp_path: Path) -> None:
    run = run_plan(short_plan, output_dir=tmp_path / "run")
    assert run.artifact_dir is not None
    stale = run.status.model_copy(update={"completed_job_ids": (), "state": "running"})
    manifest_path = run.artifact_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["status"] = stale.model_dump(mode="json")
    manifest_path.write_text(json.dumps(manifest))
    recovered = load_run(run.artifact_dir)
    assert recovered.results == run.results
    assert recovered.status.completed_job_ids == run.status.completed_job_ids
    assert recovered.status.lost_job_ids == ()


def test_unknown_record_versions_are_rejected(short_plan: ArenaPlan, tmp_path: Path) -> None:
    run = run_plan(short_plan, output_dir=tmp_path / "run")
    assert run.artifact_dir is not None
    result_path = run.artifact_dir / "results.jsonl"
    lines = result_path.read_text().splitlines()
    invalid = json.loads(lines[0])
    invalid["record_version"] = 999
    result_path.write_text("\n".join([json.dumps(invalid), *lines[1:]]) + "\n")
    with pytest.raises(ValueError, match="record_version"):
        load_run(run.artifact_dir)


@pytest.mark.arena_slow
def test_planned_volume_matches_variable_player_count_process_results(tmp_path: Path) -> None:
    plan = build_arena_plan(
        LineupConfig(
            catalogue=(
                CandidateConfig(bot="random"),
                CandidateConfig(bot="closest_gap"),
                CandidateConfig(bot="controlled_burn", options={"K": 5, "fallback_strategy": "closest_gap"}),
            ),
            player_counts=(2, 3, 4, 5, 6, 10),
            games=10,
            rotations=False,
            protocol=MatchProtocol(end_condition="fixed_hands", hands=3, anonymise_display_names=True),
        )
    )
    sequential = run_plan(plan, output_dir=tmp_path / "thread")
    parallel = run_plan(
        plan, config=replace(plan.execution, backend="process", concurrency=4), output_dir=tmp_path / "process"
    )
    assert len(sequential.results) == len(parallel.results) == 60
    assert [_facts(record) for record in sequential.results] == [_facts(record) for record in parallel.results]
