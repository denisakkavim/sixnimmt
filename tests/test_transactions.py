"""Bot transactions publish all effects together only after engine validation."""

import pytest

from sixnimmt_server.arena.bots import GreedyBot
from sixnimmt_server.arena.bots.base import ActionBatch, Rejection
from sixnimmt_server.arena.runner import RunConfig, run_match
from sixnimmt_server.engine.actions import Action, CommitAction, SelectCardAction, SendMessageAction
from sixnimmt_server.engine.rules import MatchProtocol
from sixnimmt_server.engine.views import MatchView


class TransactionBot:
    def __init__(self, invalid: str | None = None, memory_only: bool = False) -> None:
        self.memory = "old notes"
        self.invalid = invalid
        self.memory_only = memory_only
        self.offers: list[tuple[MatchView, Rejection | None, str]] = []

    def act(self, view: MatchView, rejection: Rejection | None = None) -> ActionBatch:
        self.offers.append((view, rejection, self.memory))
        if self.memory_only:
            return ActionBatch((), "new notes")
        card = min(view.you.hand)
        actions: tuple[Action, ...] = (SelectCardAction(card=card), CommitAction())
        if rejection is None and self.invalid == "card":
            missing = next(value for value in range(1, 105) if value not in view.you.hand)
            actions = (SendMessageAction(visibility="table", body="must not escape"), SelectCardAction(card=missing))
        if rejection is None and self.invalid == "commit_first":
            actions = (CommitAction(), SelectCardAction(card=card))
        if rejection is None and self.invalid == "after_commit":
            actions = (*actions, SendMessageAction(visibility="table", body="must not escape"))
        return ActionBatch(actions, "new notes")

    def accept_batch(self, batch: ActionBatch) -> None:
        if batch.memory is not None:
            self.memory = batch.memory


@pytest.mark.parametrize("invalid", ["card", "commit_first", "after_commit"])
def test_rejected_transaction_preserves_state_and_memory_before_retry(invalid: str) -> None:
    bot = TransactionBot(invalid)
    failed_size = 4 if invalid == "after_commit" else 3
    result = run_match(
        [bot, GreedyBot()],
        123,
        protocol=MatchProtocol(communication_enabled=True),
        config=RunConfig(match_action_limit=failed_size + 3),
    )
    first_view, _, _ = bot.offers[0]
    retry_view, rejection, memory = bot.offers[1]
    assert memory == "old notes"
    assert retry_view.you.hand == first_view.you.hand
    assert retry_view.you.selection is None
    assert not retry_view.you.committed
    assert retry_view.rows == first_view.rows
    assert not retry_view.messages
    assert rejection is not None
    assert "Nothing was applied; memory is unchanged" in rejection.message
    assert all("must not escape" not in str(event.data) for event in result.events)
    assert result.actions_rejected == failed_size
    assert result.actions_accepted == 3
    assert bot.memory == "new notes"


def test_transaction_exceeding_remaining_budget_applies_nothing() -> None:
    bot = TransactionBot()
    result = run_match(
        [bot, GreedyBot()],
        123,
        protocol=MatchProtocol(communication_enabled=True),
        config=RunConfig(match_action_limit=2),
    )
    assert result.reason == "match_action_limit"
    assert result.actions_accepted == 0
    assert bot.memory == "old notes"
    assert result.final_state.players[0].selection is None


def test_memory_only_transactions_are_bounded_by_play_limit() -> None:
    bot = TransactionBot(memory_only=True)
    result = run_match(
        [bot, TransactionBot(memory_only=True)],
        123,
        protocol=MatchProtocol(communication_enabled=True),
        config=RunConfig(play_action_limit=3),
    )
    assert result.reason == "play_action_limit"
    assert result.actions_accepted == 3
    assert bot.memory == "new notes"


def test_failed_transactions_count_toward_rejection_limit() -> None:
    class AlwaysInvalid(TransactionBot):
        def act(self, view: MatchView, rejection: Rejection | None = None) -> ActionBatch:
            return super().act(view)

    bot = AlwaysInvalid("card")
    result = run_match(
        [bot, GreedyBot()],
        123,
        protocol=MatchProtocol(communication_enabled=True),
        config=RunConfig(decision_rejection_limit=2),
    )
    assert result.outcome == "forfeited"
    assert result.actions_rejected == 6
    assert bot.memory == "old notes"
    assert not any(event.type == "message_sent" for event in result.events)


def test_transaction_traces_share_offered_view_and_count_latency_once(tmp_path) -> None:
    import json

    directory = tmp_path / "transaction"
    result = run_match(
        [TransactionBot(), GreedyBot()],
        123,
        protocol=MatchProtocol(communication_enabled=True),
        config=RunConfig(match_action_limit=3, trace_dir=directory),
    )
    records = [json.loads(line) for line in (directory / "arena_0.actions.jsonl").read_text().splitlines()]
    assert result.actions_accepted == 3
    assert len({record["from_view"] for record in records}) == 1
    assert sum(record["decision_duration_ms"] is not None for record in records) == 1
    assert [record["type"] for record in records] == ["select_card", "commit", "update_memory"]
