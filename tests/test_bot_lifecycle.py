"""Managed bot resources and decision receipts follow arena publication."""

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sixnimmt.arena.bots.base import ActionBatch, Rejection
from sixnimmt.arena.bots.composed import ControlledBurnBot, HandAwareRowChoiceBot
from sixnimmt.arena.bots.external import HarnessBot
from sixnimmt.arena.bots.external_harnesses.broker import SeatSession
from sixnimmt.arena.bots.heuristics import RandomBot
from sixnimmt.arena.bots.lifecycle import (
    BotContext,
    BotMatchEnd,
    ControllerStopped,
    DecisionContext,
    DecisionDeadlineExceeded,
    DecisionOutcome,
)
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.match import run_match
from sixnimmt.arena.results import MatchOutcome
from sixnimmt.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.audience import Viewer
from sixnimmt.engine.events import Event
from sixnimmt.engine.fold import build_view
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.state import MatchState
from sixnimmt.engine.views import MatchView, ViewRole
from sixnimmt.persistence.sink import ActionRecord, NullEventSink, read_action_log


class RecordingBot:
    def __init__(self, seed: int = 1) -> None:
        self.delegate = RandomBot(seed)
        self.starts: list[BotContext] = []
        self.contexts: list[DecisionContext] = []
        self.views: list[MatchView] = []
        self.receipts: list[DecisionOutcome] = []
        self.ends: list[BotMatchEnd | None] = []
        self.cancellations: list[str] = []
        self.order: list[str] = []
        self.call_threads: list[int] = []

    def start(self, context: BotContext) -> None:
        self.starts.append(context)
        self.order.append("start")

    def set_decision_context(self, context: DecisionContext) -> None:
        self.contexts.append(context)
        self.order.append("context")

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action | ActionBatch:
        self.views.append(view)
        self.order.append("act")
        self.call_threads.append(threading.get_ident())
        return self.delegate.act(view, rejection)

    def settle_decision(self, outcome: DecisionOutcome) -> None:
        self.receipts.append(outcome)
        self.order.append(outcome.status)

    def cancel(self, reason: str) -> None:
        self.cancellations.append(reason)

    def close(self, end: BotMatchEnd | None) -> None:
        self.ends.append(end)
        self.order.append("close")

    def stats(self) -> dict[str, Any]:
        return {"closed": len(self.ends), "receipts": len(self.receipts)}


class RejectOnceBot(RecordingBot):
    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action | ActionBatch:
        if len(self.receipts) == 0:
            return ChooseRowAction(row_index=99)
        return super().act(view, rejection)


class BatchBot(RecordingBot):
    def act(self, view: MatchView, rejection: Rejection | None = None) -> ActionBatch:
        return ActionBatch((SelectCardAction(card=view.you.hand[0]), CommitAction()))


class FailingStartBot(RecordingBot):
    def start(self, context: BotContext) -> None:
        super().start(context)
        msg = "private startup detail"
        raise ValueError(msg)


class FailingCloseBot(RecordingBot):
    def close(self, end: BotMatchEnd | None) -> None:
        super().close(end)
        msg = "cleanup detail"
        raise RuntimeError(msg)


class StoppingBot(RecordingBot):
    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        raise ControllerStopped


class ExpiredBot(RecordingBot):
    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        raise DecisionDeadlineExceeded


class BlockingBot(RecordingBot):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.returned = threading.Event()

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action | ActionBatch:
        self.entered.set()
        self.release.wait(5)
        action = super().act(view, rejection)
        self.returned.set()
        return action

    def cancel(self, reason: str) -> None:
        super().cancel(reason)
        self.release.set()


class FailingActionSink(NullEventSink):
    def record_action(self, record: ActionRecord) -> None:
        msg = "action storage failed"
        raise OSError(msg)


def _fail_selection_observer(state: MatchState, events: tuple[Event, ...]) -> None:
    if any(event.type == "selection_made" for event in events):
        msg = "observer failed after publication"
        raise RuntimeError(msg)


def _stop_entered_call(entered: threading.Event, stop: threading.Event) -> None:
    entered.wait(2)
    stop.set()


@pytest.fixture
def short_protocol() -> MatchProtocol:
    return MatchProtocol(end_condition="fixed_hands", hands=1)


def test_settles_final_decision_before_closing_match(short_protocol: MatchProtocol) -> None:
    bots = [RecordingBot(1), RecordingBot(2)]
    result = run_match(bots, 123, protocol=short_protocol)

    assert result.outcome == MatchOutcome.FINISHED
    assert sum(len(bot.receipts) for bot in bots) == result.actions_accepted
    for bot in bots:
        assert bot.order[0] == "start"
        assert bot.order[-2:] == ["accepted", "close"]
        assert len(bot.starts) == 1
        assert len(bot.ends) == 1
        assert bot.ends[0] is not None
        assert bot.ends[0].outcome == MatchOutcome.FINISHED
        assert bot.ends[0].winners == result.winners


def test_final_views_remain_filtered_for_each_seat() -> None:
    bots = [RecordingBot(1), RecordingBot(2)]
    result = run_match(bots, 123, config=RunConfig(match_action_limit=1))

    for index, bot in enumerate(bots):
        player_id = f"player_{index + 1}"
        end = bot.ends[0]
        assert end is not None
        assert end.final_view == build_view(result.events, Viewer(ViewRole.PLAYER, player_id))
        assert end.reason == "match_action_limit"
        assert dict(end.scores) == {"player_1": 0, "player_2": 0}
        assert bot.starts[0].player_id == player_id


def test_supplies_deadline_before_calling_bot() -> None:
    bot = RecordingBot()
    before = datetime.now(UTC)
    run_match([bot, RandomBot(2)], 123, config=RunConfig(match_action_limit=1, decision_timeout_seconds=1))

    assert bot.order[:3] == ["start", "context", "act"]
    assert len(bot.contexts) == 1
    deadline = bot.contexts[0].deadline
    assert deadline is not None
    assert deadline.tzinfo == UTC
    assert deadline > before


def test_untimed_lifecycle_keeps_bot_calls_inline() -> None:
    bot = RecordingBot()
    run_match([bot, RandomBot(2)], 123, config=RunConfig(match_action_limit=1))

    assert bot.contexts == [DecisionContext(None)]
    assert bot.call_threads == [threading.get_ident()]


def test_rejection_is_settled_with_refreshed_legal_actions() -> None:
    bot = RejectOnceBot()
    result = run_match([bot, RandomBot(2)], 123, config=RunConfig(match_action_limit=2))

    assert [receipt.status for receipt in bot.receipts] == ["rejected", "accepted"]
    rejection = bot.receipts[0].rejection
    assert rejection is not None
    assert rejection.legal_actions == bot.views[0].legal_actions
    assert result.actions_rejected == 1


def test_invalid_batch_gets_one_rejection_receipt_without_acceptance() -> None:
    bot = BatchBot()
    result = run_match([bot, RandomBot(2)], 123, config=RunConfig(decision_rejection_limit=1))

    assert result.outcome == MatchOutcome.FORFEITED
    assert result.actions_accepted == 0
    assert len(bot.receipts) == 1
    assert bot.receipts[0].status == "rejected"
    assert bot.ends[0] is not None
    assert bot.ends[0].reason == result.reason


def test_accepted_batch_gets_one_receipt_for_all_published_actions() -> None:
    bot = BatchBot()
    result = run_match(
        [bot, RandomBot(2)],
        123,
        protocol=MatchProtocol(communication_enabled=True),
        config=RunConfig(match_action_limit=2),
    )

    assert result.actions_accepted == 2
    assert bot.receipts == [DecisionOutcome("accepted")]
    assert bot.order[-2:] == ["accepted", "close"]


def test_batch_exceeding_attempt_budget_is_settled_as_failed() -> None:
    bot = BatchBot()
    result = run_match(
        [bot, RandomBot(2)],
        123,
        protocol=MatchProtocol(communication_enabled=True),
        config=RunConfig(match_action_limit=1),
    )

    assert result.outcome == MatchOutcome.ABANDONED
    assert result.actions_accepted == 0
    assert bot.receipts == [DecisionOutcome("failed", reason="match_action_limit")]


def test_observer_failure_does_not_acknowledge_acceptance() -> None:
    bots = [RecordingBot(1), RecordingBot(2)]
    with pytest.raises(RuntimeError, match="observer failed"):
        run_match(bots, 123, observer=_fail_selection_observer)

    assert bots[0].receipts == [DecisionOutcome("failed", reason="publication_failed")]
    assert all(bot.ends == [None] for bot in bots)
    assert all(bot.cancellations == ["match_failed"] for bot in bots)


def test_action_storage_failure_closes_started_bots() -> None:
    bots = [RecordingBot(1), RecordingBot(2)]
    with pytest.raises(OSError, match="action storage failed"):
        run_match(bots, 123, sink=FailingActionSink())

    assert bots[0].receipts == [DecisionOutcome("failed", reason="publication_failed")]
    assert all(bot.ends == [None] for bot in bots)


def test_partial_start_failure_closes_all_owned_seats_and_sanitizes_reason() -> None:
    bots = [RecordingBot(1), FailingStartBot(2), RecordingBot(3)]
    result = run_match(bots, 123)

    assert result.outcome == MatchOutcome.FAILED
    assert [len(bot.ends) for bot in bots] == [1, 1, 1]
    assert bots[2].ends[0] is None
    assert bots[0].ends[0] is not None
    assert bots[0].ends[0].reason == "match_failed"
    assert result.lifecycle_errors[0][:2] == ("player_2", "start")


def test_cleanup_errors_preserve_outcome_and_reach_manifest(tmp_path: Path, short_protocol: MatchProtocol) -> None:
    bot = FailingCloseBot()
    directory = tmp_path / "trace"
    result = run_match([bot, RandomBot(2)], 123, protocol=short_protocol, config=RunConfig(trace_dir=directory))

    assert result.outcome == MatchOutcome.FINISHED
    assert result.lifecycle_errors == (("player_1", "close", "RuntimeError('cleanup detail')"),)
    manifest = json.loads((directory / "manifest.json").read_text())
    entry = manifest["matches"][0]
    assert entry["seat_stats"]["player_1"]["closed"] == 1
    assert "cleanup detail" in entry["stats_errors"]["player_1"]


def test_composed_strategy_keeps_outer_policy_and_forwards_context() -> None:
    delegate = RecordingBot()
    bot = ControlledBurnBot(K=99, fallback=delegate)
    result = run_match([bot, RandomBot(2)], 1, config=RunConfig(match_action_limit=1))

    assert result.actions_accepted == 1
    assert len(delegate.contexts) == 1
    assert len(delegate.views) == 0
    assert delegate.receipts == [DecisionOutcome("accepted")]
    assert len(delegate.ends) == 1


def test_shared_delegate_is_rejected_and_closed_once() -> None:
    delegate = RecordingBot()
    bots = [ControlledBurnBot(K=0, fallback=delegate), HandAwareRowChoiceBot(max_extra_penalty=0, card_bot=delegate)]
    result = run_match(bots, 123)

    assert result.outcome == MatchOutcome.FAILED
    assert len(delegate.starts) == 1
    assert len(delegate.ends) == 1
    assert len(delegate.views) == 0


def test_distinct_adapters_cannot_share_one_harness_session() -> None:
    session = SeatSession("player_1", "A", GameRules(), MatchProtocol())
    result = run_match([HarnessBot(session), HarnessBot(session)], 123)

    assert result.outcome == MatchOutcome.FAILED
    assert "shared between seats" in result.lifecycle_errors[0][2]
    terminal = session.play(session.session_id)
    assert terminal["status"] == "terminal"
    final_view = terminal["result"]["final_view"]
    assert final_view is not None
    assert MatchView.model_validate(final_view).you.player_id == "player_1"


def test_controller_stop_is_abandonment_without_a_move() -> None:
    bot = StoppingBot()
    result = run_match([bot, RandomBot(2)], 123)

    assert result.outcome == MatchOutcome.ABANDONED
    assert result.reason == "operator_stop"
    assert result.actions_accepted == 0
    assert bot.cancellations == ["operator_stop"]
    assert bot.receipts == [DecisionOutcome("failed", reason="operator_stop")]


def test_managed_deadline_uses_timeout_classification(tmp_path: Path) -> None:
    bot = ExpiredBot()
    directory = tmp_path / "trace"
    result = run_match([bot, RandomBot(2)], 123, config=RunConfig(trace_dir=directory))

    assert result.reason == "decision_timeout"
    assert result.outcome == MatchOutcome.FAILED
    assert bot.cancellations == ["decision_timeout"]
    assert read_action_log(directory / "arena_0.actions.jsonl")[0].outcome == "timeout"


def test_timeout_cancels_work_and_discards_late_move() -> None:
    bot = BlockingBot()
    try:
        result = run_match([bot, RandomBot(2)], 123, config=RunConfig(decision_timeout_seconds=0.02))
        assert bot.returned.wait(1)
        assert result.outcome == MatchOutcome.FAILED
        assert result.actions_accepted == 0
        assert bot.cancellations == ["decision_timeout"]
        assert bot.receipts == [DecisionOutcome("failed", reason="decision_timeout")]
        assert len(bot.ends) == 1
    finally:
        bot.release.set()


def test_stop_event_interrupts_bot_without_a_decision_deadline() -> None:
    bot = BlockingBot()
    stop = threading.Event()
    stopper = threading.Thread(target=_stop_entered_call, args=(bot.entered, stop), daemon=True)
    stopper.start()
    try:
        result = run_match([bot, RandomBot(2)], 123, stop_event=stop)
        assert bot.entered.is_set()
        assert result.outcome == MatchOutcome.ABANDONED
        assert result.reason == "operator_stop"
        assert result.actions_accepted == 0
        assert bot.cancellations == ["operator_stop"]
    finally:
        stop.set()
        bot.release.set()
        stopper.join(timeout=2)
