"""Cancellation and optional watching must preserve ordinary batch execution."""

from functools import partial
from pathlib import Path
from threading import Event as ThreadEvent
from threading import get_ident

import pytest

from sixnimmt.arena.artifacts import load_run
from sixnimmt.arena.bots.base import BotSpec, Rejection
from sixnimmt.arena.bots.heuristics import RandomBot
from sixnimmt.arena.bots.registry import REGISTRY
from sixnimmt.arena.config import Backend, RunConfig
from sixnimmt.arena.execution import run_plan
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.arena.results import MatchOutcome
from sixnimmt.engine.actions import Action
from sixnimmt.engine.events import Event
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.engine.state import MatchState
from sixnimmt.engine.views import MatchView
from tests.run_helpers import fixed_plan


class ThreadProbeBot(RandomBot):
    def __init__(self, seed: int, *, threads: set[int]) -> None:
        super().__init__(seed)
        self.threads = threads

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        self.threads.add(get_ident())
        return super().act(view, rejection)


def record_observer_thread(threads: set[int], state: MatchState, events: tuple[Event, ...]) -> None:
    threads.add(get_ident())


def test_unwatched_run_without_deadline_calls_bots_in_match_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    decisions: set[int] = set()
    observations: set[int] = set()
    factory = partial(ThreadProbeBot, threads=decisions)
    monkeypatch.setitem(REGISTRY, "thread_probe", BotSpec("thread_probe", factory, True, {}))
    plan = fixed_plan(
        [PlayerConfig(bot="thread_probe"), PlayerConfig(bot="random")],
        1,
        66,
        protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
    )
    result = run_plan(plan, observer=partial(record_observer_thread, observations))
    assert result.results[0].outcome == MatchOutcome.FINISHED
    assert len(decisions) == len(observations) == 1
    assert decisions == observations


@pytest.mark.parametrize("backend", ["thread", "process"])
def test_cancelled_run_records_unstarted_jobs_without_launching_them(tmp_path: Path, backend: Backend) -> None:
    stopped = ThreadEvent()
    stopped.set()
    plan = fixed_plan([PlayerConfig(bot="random")] * 2, 3, 66, config=RunConfig(backend=backend))
    result = run_plan(plan, stop_event=stopped, output_dir=tmp_path / "cancelled")
    assert result.status.state == "stopped"
    assert result.status.error == "operator_stop"
    assert result.status.started_job_ids == ()
    assert result.status.unstarted_job_ids == tuple(job.job_id for job in plan.jobs)
    assert result.results == ()
    assert load_run(tmp_path / "cancelled") == result
