"""Real arena scheduling through blocking harness sessions, without model calls."""

from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Queue
from threading import Event, Thread
from typing import Any
from uuid import uuid4

import pytest

from sixnimmt.arena.bots.agent_contract import action_tools, observation_text
from sixnimmt.arena.bots.external import HarnessBot
from sixnimmt.arena.bots.external_harnesses.broker import SeatSession
from sixnimmt.arena.bots.external_harnesses.protocol import HarnessError
from sixnimmt.arena.bots.heuristics import LowestFittingCardBot
from sixnimmt.arena.results import MatchOutcome, MatchResult
from sixnimmt.arena.runner import RunConfig, run_match
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.state import PlayerSeat
from sixnimmt.engine.views import MatchView


def proposal_for(offer: dict[str, Any], actions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if actions is None:
        view = MatchView.model_validate(offer["view"])
        action = LowestFittingCardBot().act(view)
        actions = [action.model_dump(mode="json", exclude={"action_id", "from_view", "expected_view_version"})]
    return {
        "protocol_version": 1,
        "session_id": offer["session_id"],
        "decision_id": offer["decision_id"],
        "submission_id": uuid4().hex,
        "view_id": offer["view_id"],
        "actions": actions,
        "memory": None,
    }


class RunningTable:
    def __init__(
        self,
        *,
        communication: bool = False,
        baseline_opponent: bool = False,
        memory: bool = False,
        decision_timeout: float = 5,
    ) -> None:
        self.rules = GameRules()
        self.protocol = MatchProtocol(end_condition="fixed_hands", hands=1, communication_enabled=communication)
        self.a = SeatSession("a", "A", self.rules, self.protocol, wait_timeout_seconds=2, memory_enabled=memory)
        self.b = SeatSession("b", "B", self.rules, self.protocol, wait_timeout_seconds=2)
        self.bots = [HarnessBot(self.a), LowestFittingCardBot() if baseline_opponent else HarnessBot(self.b)]
        self.stopped = Event()
        self.decision_timeout = decision_timeout
        self.results: Queue[MatchResult | BaseException] = Queue()
        self.thread = Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self) -> None:
        try:
            result = run_match(
                self.bots,
                seed=123,
                match_id="harness-test",
                seats=[PlayerSeat(player_id="a"), PlayerSeat(player_id="b")],
                rules=self.rules,
                protocol=self.protocol,
                config=RunConfig(decision_timeout_seconds=self.decision_timeout),
                stop_event=self.stopped,
            )
            self.results.put(result)
        except BaseException as error:
            self.results.put(error)

    def close(self) -> None:
        self.stopped.set()
        self.a.stop()
        self.b.stop()
        self.thread.join(timeout=3)
        self.a.close(None)
        self.b.close(None)


@pytest.fixture
def table() -> Iterator[RunningTable]:
    running = RunningTable()
    try:
        yield running
    finally:
        running.close()


def test_game_info_does_not_expose_a_deal_or_mark_ready(table: RunningTable) -> None:
    info = table.a.get_game_info(table.a.session_id)
    assert "view" not in info
    assert "seed" not in info
    assert "match_id" not in info
    assert not table.a.ready.is_set()


def test_offer_uses_existing_llm_observation_and_action_schemas(table: RunningTable) -> None:
    offer = table.a.play(table.a.session_id)["offer"]
    view = MatchView.model_validate(offer["view"])
    assert offer["observation"] == observation_text(view)
    assert offer["action_tools"] == action_tools(view, strict=False)
    assert all("hand" not in player for player in offer["view"]["players"])
    assert "seed" not in offer["view"]


def test_recovery_returns_same_unsubmitted_offer_and_deadline(table: RunningTable) -> None:
    first = table.a.play(table.a.session_id)
    recovered = table.a.play(table.a.session_id)
    assert recovered == first
    assert first["offer"]["deadline"] is not None


def test_staging_advances_arena_while_submitter_waits(table: RunningTable) -> None:
    first = table.a.play(table.a.session_id)["offer"]
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(table.a.play, table.a.session_id, proposal_for(first))
        other_offer = table.b.play(table.b.session_id)["offer"]
        assert other_offer["view"]["you"]["player_id"] == "b"
        assert not pending.done()
        table.stopped.set()
        table.a.stop()
        table.b.stop()
        response = pending.result(timeout=3)
    assert response["status"] == "terminal"
    assert response["receipt"]["status"] == "accepted"
    assert response["result"]["outcome"] == "abandoned"


def test_duplicate_during_wait_does_not_stage_another_proposal(table: RunningTable) -> None:
    first = table.a.play(table.a.session_id)["offer"]
    proposal = proposal_for(first)
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(table.a.play, table.a.session_id, proposal)
        table.b.play(table.b.session_id)
        duplicate = table.a.play(table.a.session_id, proposal)
        assert duplicate["status"] == "already_waiting"
        assert duplicate["receipt"]["submission_id"] == proposal["submission_id"]
        table.a.stop()
        table.b.stop()
        pending.result(timeout=3)
    assert table.a.stats()["submissions"] == 1


def test_rpc_cancellation_keeps_published_move_and_allows_recovery(table: RunningTable) -> None:
    first = table.a.play(table.a.session_id)["offer"]
    cancel = Event()
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(table.a.play, table.a.session_id, proposal_for(first), cancel)
        table.b.play(table.b.session_id)
        cancel.set()
        with pytest.raises(HarnessError, match="cancelled"):
            pending.result(timeout=1)
    table.a.stop()
    table.b.stop()
    recovered = table.a.play(table.a.session_id)
    assert recovered["status"] == "terminal"
    assert recovered["receipt"]["status"] == "accepted"


def test_rejection_returns_new_offer_without_applying_memory() -> None:
    table = RunningTable(memory=True)
    try:
        first = table.a.play(table.a.session_id)["offer"]
        missing = next(card for card in range(1, 105) if card not in first["view"]["you"]["hand"])
        proposal = proposal_for(first, [{"type": "select_card", "card": missing}])
        proposal["memory"] = "This must not stick."
        correction = table.a.play(table.a.session_id, proposal)
        assert correction["status"] == "decision"
        assert correction["receipt"]["status"] == "rejected"
        assert correction["offer"]["decision_id"] != first["decision_id"]
        assert correction["offer"]["rejection"] is not None
        assert correction["offer"]["memory"] is None
    finally:
        table.close()


def test_completed_retry_uses_current_offer_and_original_receipt() -> None:
    table = RunningTable(baseline_opponent=True)
    try:
        first = table.a.play(table.a.session_id)["offer"]
        proposal = proposal_for(first)
        response = table.a.play(table.a.session_id, proposal)
        recovered = table.a.play(table.a.session_id, proposal)
        assert recovered == response
        assert recovered["offer"]["decision_id"] != first["decision_id"]
        conflicting = {**proposal, "memory": "different"}
        with pytest.raises(HarnessError) as error:
            table.a.play(table.a.session_id, conflicting)
        assert error.value.code == "submission_conflict"
    finally:
        table.close()


def test_mismatched_session_cannot_read_or_submit(table: RunningTable) -> None:
    with pytest.raises(HarnessError) as error:
        table.a.get_game_info(table.b.session_id)
    assert error.value.code == "invalid_session"
    first = table.a.play(table.a.session_id)["offer"]
    proposal = proposal_for(first)
    proposal["session_id"] = table.b.session_id
    with pytest.raises(HarnessError) as error:
        table.a.play(table.a.session_id, proposal)
    assert error.value.code == "invalid_session"
    assert table.a.stats()["submissions"] == 0


def test_wait_expiry_returns_receipt_and_preserves_submission(table: RunningTable) -> None:
    table.a.wait_timeout_seconds = 0.02
    first = table.a.play(table.a.session_id)["offer"]
    proposal = proposal_for(first)
    expired = table.a.play(table.a.session_id, proposal)
    assert expired["status"] == "wait_expired"
    assert expired["receipt"]["status"] == "accepted"
    recovered = table.a.play(table.a.session_id)
    assert recovered["status"] == "wait_expired"
    assert recovered["offer"] is None
    assert recovered["receipt"]["submission_id"] == proposal["submission_id"]
    assert table.a.stats()["submissions"] == 1


def test_stale_view_is_rejected_without_changing_current_offer(table: RunningTable) -> None:
    first = table.a.play(table.a.session_id)["offer"]
    proposal = proposal_for(first)
    proposal["view_id"] = "old-view"
    with pytest.raises(HarnessError) as error:
        table.a.play(table.a.session_id, proposal)
    assert error.value.code == "stale_decision"
    assert table.a.play(table.a.session_id)["offer"] == first
    assert table.a.stats()["submissions"] == 0


def test_cancelled_request_before_staging_has_no_game_effect(table: RunningTable) -> None:
    first = table.a.play(table.a.session_id)["offer"]
    cancel = Event()
    cancel.set()
    with pytest.raises(HarnessError) as error:
        table.a.play(table.a.session_id, proposal_for(first), cancel)
    assert error.value.code == "cancelled"
    assert table.a.play(table.a.session_id)["offer"] == first
    assert table.a.stats()["submissions"] == 0


def test_repeated_protocol_errors_end_decision_without_an_unbounded_retry_loop(table: RunningTable) -> None:
    table.a.play(table.a.session_id)
    for _ in range(8):
        with pytest.raises(HarnessError):
            table.a.play(table.a.session_id, {})
    terminal = table.a.play(table.a.session_id)
    assert terminal["status"] == "terminal"
    assert terminal["result"]["outcome"] == "failed"
    assert table.a.stats()["submissions"] == 0


def test_decision_timeout_closes_offer_and_wakes_waiting_harness() -> None:
    table = RunningTable(decision_timeout=0.05)
    try:
        first = table.a.play(table.a.session_id)["offer"]
        result = table.results.get(timeout=2)
        assert isinstance(result, MatchResult)
        assert result.outcome == MatchOutcome.FAILED
        terminal = table.a.play(table.a.session_id)
        assert terminal["status"] == "terminal"
        assert terminal["offer"] is None
        with pytest.raises(HarnessError) as error:
            table.a.play(table.a.session_id, proposal_for(first))
        assert error.value.code == "session_closed"
    finally:
        table.close()


def test_accepted_notebook_is_in_next_offer() -> None:
    table = RunningTable(baseline_opponent=True, memory=True)
    try:
        first = table.a.play(table.a.session_id)["offer"]
        proposal = proposal_for(first)
        proposal["memory"] = "Accepted note."
        response = table.a.play(table.a.session_id, proposal)
        assert response["receipt"]["status"] == "accepted"
        assert response["offer"]["memory"] == "Accepted note."
    finally:
        table.close()


def play_to_end(session: SeatSession) -> dict[str, Any]:
    response = session.play(session.session_id)
    for _ in range(100):
        if response["status"] == "terminal":
            return response
        if response["status"] == "wait_expired":
            response = session.play(session.session_id)
            continue
        assert response["status"] == "decision"
        response = session.play(session.session_id, proposal_for(response["offer"]))
    pytest.fail("Harness did not finish within its decision bound")


def test_terminal_recovery_preserves_receipts_but_rejects_new_proposals() -> None:
    table = RunningTable(baseline_opponent=True)
    try:
        first = table.a.play(table.a.session_id)["offer"]
        proposal = proposal_for(first)
        table.a.play(table.a.session_id, proposal)
        terminal = play_to_end(table.a)
        retry = table.a.play(table.a.session_id, proposal)
        assert retry["status"] == "terminal"
        assert retry["result"] == terminal["result"]
        assert retry["receipt"]["submission_id"] == proposal["submission_id"]
        assert retry["receipt"]["status"] == "accepted"
        unknown = {**proposal, "submission_id": "unknown"}
        with pytest.raises(HarnessError) as error:
            table.a.play(table.a.session_id, unknown)
        assert error.value.code == "session_closed"
    finally:
        table.close()


@pytest.mark.parametrize("communication", [False, True], ids=["classic", "communication"])
def test_two_harnesses_finish_with_final_receipts_and_baseline_equivalent_state(communication: bool) -> None:
    table = RunningTable(communication=communication)
    futures: list[Future[dict[str, Any]]] = []
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(play_to_end, session) for session in (table.a, table.b)]
            responses = [future.result(timeout=10) for future in futures]
        result = table.results.get(timeout=2)
        assert isinstance(result, MatchResult)
        assert result.outcome == MatchOutcome.FINISHED
        for response in responses:
            assert response["result"]["outcome"] == "finished"
            assert response["receipt"]["status"] == "accepted"
            assert "seed" not in response["result"]
        baseline = run_match(
            [LowestFittingCardBot(), LowestFittingCardBot()],
            match_id="harness-test",
            seats=[PlayerSeat(player_id="a"), PlayerSeat(player_id="b")],
            seed=123,
            rules=table.rules,
            protocol=table.protocol,
        )
        assert result.final_state == baseline.final_state
    finally:
        table.close()
