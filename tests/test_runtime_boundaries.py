"""Execution adapters preserve seeds, resource ownership and public information."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sixnimmt.application import run
from sixnimmt.arena.bots.base import Bot, BotOptions, BotSpec
from sixnimmt.arena.bots.heuristics import RandomBot
from sixnimmt.arena.bots.lifecycle import BotMatchEnd
from sixnimmt.arena.bots.registry import REGISTRY
from sixnimmt.arena.config import RunConfig, resolve, resolve_settings
from sixnimmt.arena.decisions import AbandonedDecisions
from sixnimmt.arena.execution import BotAssignment, MatchJob, execute_match_job
from sixnimmt.arena.match import ArenaError, run_match
from sixnimmt.arena.planning import RunSettings
from sixnimmt.arena.players import PlayerConfig, ResolvedStrategy
from sixnimmt.arena.results import MatchOutcome
from sixnimmt.engine.events import Event
from sixnimmt.engine.replay import replay_events
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.state import MatchState, PlayerSeat
from sixnimmt.persistence.sink import read_event_log
from tests.run_helpers import run_fixed


class ResourceBot(RandomBot):
    def __init__(self, seed: int) -> None:
        super().__init__(seed)
        self.closed = 0

    def close(self, result: BotMatchEnd | None) -> None:
        self.closed += 1


@dataclass
class ResourceFactory:
    created: list[ResourceBot] = field(default_factory=list)
    fail_after: int | None = None

    def __call__(self, seed: int) -> Bot:
        if seed < 0 or (self.fail_after is not None and len(self.created) >= self.fail_after):
            msg = "private construction details"
            raise ValueError(msg)
        bot = ResourceBot(seed)
        self.created.append(bot)
        return bot


def fail_initial_observer(state: MatchState, events: tuple[Event, ...]) -> None:
    msg = "observer unavailable"
    raise RuntimeError(msg)


@pytest.mark.parametrize("fatal", [False, True])
def test_failed_lineup_construction_closes_resources_already_created(fatal: bool) -> None:
    factory = ResourceFactory()
    strategy = ResolvedStrategy(REGISTRY["random"], BotOptions(), {}, factory)
    job = MatchJob(
        "test",
        123,
        (PlayerSeat(player_id="a"), PlayerSeat(player_id="b")),
        (BotAssignment(strategy, 1), BotAssignment(strategy, -1)),
    )
    if fatal:
        with pytest.raises(ArenaError, match="cannot construct"):
            execute_match_job(
                job, GameRules(), MatchProtocol(), RunConfig(), AbandonedDecisions(4), construction_fatal=True
            )
    else:
        executed = execute_match_job(job, GameRules(), MatchProtocol(), RunConfig(), AbandonedDecisions(4))
        assert executed.result.outcome == MatchOutcome.FAILED
        assert executed.result.ended_by == "b"
    assert [bot.closed for bot in factory.created] == [1]


def test_job_closes_supplied_resources_when_initial_observer_fails() -> None:
    bots = [ResourceBot(1), ResourceBot(2)]
    job = MatchJob("test", 123, (PlayerSeat(player_id="a"), PlayerSeat(player_id="b")))
    with pytest.raises(RuntimeError, match="observer unavailable"):
        execute_match_job(
            job,
            GameRules(),
            MatchProtocol(),
            RunConfig(),
            AbandonedDecisions(4),
            initial_bots=bots,
            observer=fail_initial_observer,
        )
    assert [bot.closed for bot in bots] == [1, 1]


def test_direct_match_closes_resources_when_trace_setup_fails(tmp_path: Path) -> None:
    bots = [ResourceBot(1), ResourceBot(2)]
    with pytest.raises(FileExistsError):
        run_match(bots, 123, config=RunConfig(trace_dir=tmp_path))
    assert [bot.closed for bot in bots] == [1, 1]


def test_fixed_arena_closes_partially_constructed_lineup(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = ResourceFactory(fail_after=1)
    monkeypatch.setitem(REGISTRY, "resource", BotSpec("resource", factory, True, {}))
    result = run_fixed([PlayerConfig(bot="resource"), PlayerConfig(bot="resource")], 1, 123)
    assert result.results[0].outcome == "failed"
    assert result.results[0].ended_by == 1
    assert [bot.closed for bot in factory.created] == [1]


def test_fixed_run_does_not_construct_resources_when_output_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = ResourceFactory()
    monkeypatch.setitem(REGISTRY, "resource", BotSpec("resource", factory, True, {}))
    with pytest.raises(FileExistsError):
        run_fixed(
            [PlayerConfig(bot="resource"), PlayerConfig(bot="resource")],
            1,
            123,
            output_dir=tmp_path,
            trace=True,
        )
    assert factory.created == []


def test_fixed_arena_closes_each_resource_once_across_setup_and_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = ResourceFactory()
    monkeypatch.setitem(REGISTRY, "resource", BotSpec("resource", factory, True, {}))
    result = run_fixed(
        [PlayerConfig(bot="resource"), PlayerConfig(bot="resource")],
        2,
        123,
        protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
        config=RunConfig(concurrency=2),
    )
    assert len(result.results) == 2
    assert [bot.closed for bot in factory.created] == [1, 1, 1, 1]


def test_python_run_records_replayable_match_with_planned_seeds(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    settings = RunSettings.model_validate({
        "catalogue": [{"bot": "random"}],
        "lineup": ["random", "random"],
        "seed": 91,
        "protocol": {"end_condition": "fixed_hands", "hands": 1},
        "session": {"auto_start": True, "retain_seconds": 0},
        "display": {"watch": True, "quiet": True},
        "recording": {"output_dir": directory, "trace": True},
    })
    result = run(settings)
    job = result.run.plan.jobs[0]
    record = result.run.results[0]
    expected = run_match(
        [RandomBot(seat.bot_seed) for seat in job.seats],
        job.match_seed,
        match_id=job.match_id,
        rules=settings.rules,
        protocol=settings.protocol,
    )
    assert record.outcome == expected.outcome
    assert record.scores == tuple(player.total_score for player in expected.final_state.players)
    assert record.event_trace is not None
    replayed = replay_events(read_event_log(directory / record.event_trace))
    assert tuple(player.total_score for player in replayed.state.players) == record.scores
    assert replayed.state.match_id == job.match_id
    assert (directory / "plan.json").is_file()
    assert (directory / "results.jsonl").is_file()
    assert (directory / "manifest.json").is_file()


@pytest.mark.parametrize("field_name", ["concurrency", "match_action_limit", "decision_rejection_limit"])
@pytest.mark.parametrize("value", [True, "2", None, -1])
def test_nested_execution_rejects_invalid_required_integers(field_name: str, value: object) -> None:
    with pytest.raises(ValueError):
        RunSettings.model_validate({"execution": {field_name: value}})


@pytest.mark.parametrize("value", [True, float("inf"), float("nan"), -1])
def test_run_rejects_invalid_session_duration_before_creating_output(tmp_path: Path, value: float) -> None:
    directory = tmp_path / "run"
    with pytest.raises(ValueError):
        run(
            RunSettings.model_validate({
                "catalogue": [{"bot": "random"}],
                "lineup": ["random", "random"],
                "recording": {"output_dir": directory},
                "session": {"setup_timeout": value},
            })
        )
    assert not directory.exists()


def test_resolved_settings_have_explicit_independent_scopes() -> None:
    config = resolve(RunConfig(concurrency=3), MatchProtocol(communication_enabled=True))
    settings = resolve_settings(config, MatchProtocol(communication_enabled=True))
    assert settings.match.scheduler == "round_robin"
    assert settings.match.play_action_limit == 200
    assert settings.run.max_abandoned_decisions == 12
    assert settings.run.concurrency == 3
    assert settings.recording.trace_dir is None
