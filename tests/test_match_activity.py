"""Operational observations leave game decisions and public events unchanged."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sixnimmt.arena.bots.base import ActionBatch, Rejection
from sixnimmt.arena.bots.heuristics import LowestCardBot
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.match import run_match
from sixnimmt.arena.results import MatchOutcome
from sixnimmt.engine.actions import Action, CommitAction, SelectCardAction
from sixnimmt.engine.rules import EndCondition, MatchProtocol
from sixnimmt.engine.views import MatchView


class TracingPlayer(LowestCardBot):
    def __init__(self) -> None:
        self.callback: Callable[[dict[str, Any]], None] | None = None

    def set_trace(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self.callback = callback

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if self.callback is not None:
            self.callback({"type": "model_text", "text": "Considering the available cards."})
        return super().act(view, rejection)


def test_activity_reports_decision_start_before_model_output_and_settlement() -> None:
    activity: list[dict[str, Any]] = []
    run_match([TracingPlayer(), LowestCardBot()], 66, max_actions=1, on_activity=activity.append)
    assert [record["type"] for record in activity] == [
        "decision_started",
        "model_text",
        "decision_finished",
        "match_finished",
    ]
    assert activity[0]["player_id"] == "player_1"
    assert activity[0]["deadline"] is None
    assert activity[2]["status"] == "accepted"
    assert activity[3]["reason"] == "match_action_limit"


def test_model_output_reaches_both_live_observer_and_durable_trace(tmp_path: Path) -> None:
    activity: list[dict[str, Any]] = []
    directory = tmp_path / "traces"
    run_match(
        [TracingPlayer(), LowestCardBot()],
        66,
        config=RunConfig(trace_dir=directory, match_action_limit=1),
        on_activity=activity.append,
    )
    saved = [json.loads(line) for line in (directory / "arena_0.model.jsonl").read_text().splitlines()]
    assert saved[0]["text"] == "Considering the available cards."
    assert saved[0]["player_id"] == "player_1"
    streamed = next(record for record in activity if record["type"] == "model_text")
    assert streamed["text"] == saved[0]["text"]
    assert streamed["player_id"] == saved[0]["player_id"]


def test_observing_activity_preserves_all_game_events_and_scores() -> None:
    protocol = MatchProtocol(end_condition=EndCondition.FIXED_HANDS, hands=1)
    baseline = run_match([LowestCardBot(), LowestCardBot()], 66, protocol=protocol)
    activity: list[dict[str, Any]] = []
    observed = run_match([LowestCardBot(), LowestCardBot()], 66, protocol=protocol, on_activity=activity.append)
    assert observed.final_state == baseline.final_state
    assert [(event.type, event.data) for event in observed.events] == [
        (event.type, event.data) for event in baseline.events
    ]
    assert activity[-1]["outcome"] == "finished"
    assert activity[-1]["winners"] == observed.winners


class FailingPlayer(LowestCardBot):
    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        msg = "test failure"
        raise ValueError(msg)


def test_failed_decision_reports_failure_and_terminal_reason() -> None:
    activity: list[dict[str, Any]] = []
    run_match([FailingPlayer(), LowestCardBot()], 66, on_activity=activity.append)
    assert activity[-2]["type"] == "decision_finished"
    assert activity[-2]["status"] == "failed"
    assert "test failure" in activity[-2]["text"]
    assert activity[-1]["outcome"] == "failed"


class SelectingBatchPlayer:
    def act(self, view: MatchView, rejection: Rejection | None = None) -> ActionBatch:
        return ActionBatch((SelectCardAction(card=view.you.hand[0]), CommitAction()))


@pytest.mark.parametrize(
    ("config", "reason"),
    [(RunConfig(match_action_limit=1), "match_action_limit"), (RunConfig(play_action_limit=1), "play_action_limit")],
)
def test_unpublished_batch_reports_the_operational_limit_instead_of_acceptance(config: RunConfig, reason: str) -> None:
    activity: list[dict[str, Any]] = []
    result = run_match(
        [SelectingBatchPlayer(), LowestCardBot()],
        66,
        protocol=MatchProtocol(communication_enabled=True),
        config=config,
        on_activity=activity.append,
    )
    assert result.outcome == MatchOutcome.ABANDONED
    assert result.actions_accepted == 0
    finished = next(record for record in activity if record["type"] == "decision_finished")
    assert finished["status"] == "failed"
    assert finished["text"] == reason


class FailingMemoryPlayer:
    def act(self, view: MatchView, rejection: Rejection | None = None) -> ActionBatch:
        return ActionBatch((SelectCardAction(card=view.you.hand[0]),), memory="Remember this play.")

    def accept_batch(self, batch: ActionBatch) -> None:
        msg = "notebook write failed"
        raise ValueError(msg)


def test_memory_publication_failure_reports_failed_without_accepting_a_move() -> None:
    activity: list[dict[str, Any]] = []
    result = run_match([FailingMemoryPlayer(), LowestCardBot()], 66, on_activity=activity.append)
    assert result.outcome == MatchOutcome.FAILED
    assert result.actions_accepted == 0
    finished = next(record for record in activity if record["type"] == "decision_finished")
    assert finished["status"] == "failed"
    assert finished["text"] == result.reason
    assert "notebook write failed" in finished["text"]


class EvaluatingPlayer(LowestCardBot):
    def stats(self) -> dict[str, Any]:
        return {"candidate_values": {"18": 0.5, "45": 1.25}}


def test_candidate_scores_are_reported_as_separate_operator_activity() -> None:
    activity: list[dict[str, Any]] = []
    result = run_match([EvaluatingPlayer(), LowestCardBot()], 66, max_actions=1, on_activity=activity.append)
    evaluation = next(record for record in activity if record["type"] == "simulation_evaluation")
    assert evaluation["player_id"] == "player_1"
    assert evaluation["candidate_values"] == {"18": 0.5, "45": 1.25}
    assert all("candidate_values" not in event.data for event in result.events)


def test_committing_a_selection_does_not_report_its_cached_candidate_scores_again() -> None:
    activity: list[dict[str, Any]] = []
    result = run_match(
        [EvaluatingPlayer(), LowestCardBot()],
        66,
        max_actions=3,
        protocol=MatchProtocol(communication_enabled=True),
        on_activity=activity.append,
    )
    assert result.final_state.players[0].committed
    own_activity = [record for record in activity if record.get("player_id") == "player_1"]
    assert len([record for record in own_activity if record["type"] == "decision_finished"]) == 2
    assert len([record for record in own_activity if record["type"] == "simulation_evaluation"]) == 1
