"""Real arena scheduling through blocking harness sessions, without model calls."""

from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Queue
from threading import Event, Thread
from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from sixnimmt.arena.bots.agent_contract import action_tools, observation_text
from sixnimmt.arena.bots.external import HarnessBot
from sixnimmt.arena.bots.external_harnesses.broker import SeatSession
from sixnimmt.arena.bots.external_harnesses.messages import DecisionOffer, TerminalReply
from sixnimmt.arena.bots.external_harnesses.protocol import HarnessError
from sixnimmt.arena.bots.heuristics import LowestFittingCardBot
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.match import run_match
from sixnimmt.arena.results import MatchOutcome, MatchResult
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.state import PlayerSeat
from sixnimmt.engine.views import MatchView


def proposal_for(offer: DecisionOffer, actions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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


def agent_proposal_for(offer: DecisionOffer, actions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    legacy = proposal_for(offer, actions)
    return {
        **{key: value for key, value in legacy.items() if key not in {"protocol_version", "session_id", "view_id"}},
        "explanation": None,
    }


class RunningTable:
    def __init__(
        self,
        *,
        communication: bool = False,
        baseline_opponent: bool = False,
        memory: bool = False,
        decision_timeout: float = 5,
        seed: int = 123,
    ) -> None:
        self.rules = GameRules()
        self.protocol = MatchProtocol(end_condition="fixed_hands", hands=1, communication_enabled=communication)
        self.a = SeatSession("a", "A", self.rules, self.protocol, wait_timeout_seconds=2, memory_enabled=memory)
        self.b = SeatSession("b", "B", self.rules, self.protocol, wait_timeout_seconds=2)
        self.bots = [HarnessBot(self.a), LowestFittingCardBot() if baseline_opponent else HarnessBot(self.b)]
        self.stopped = Event()
        self.decision_timeout = decision_timeout
        self.seed = seed
        self.results: Queue[MatchResult | BaseException] = Queue()
        self.thread = Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self) -> None:
        try:
            result = run_match(
                self.bots,
                seed=self.seed,
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
    assert offer is not None
    view = MatchView.model_validate(offer["view"])
    assert offer["observation"] == observation_text(view)
    assert offer["action_tools"] == action_tools(view, strict=False)
    players = offer["view"]["players"]
    assert isinstance(players, list)
    assert all(isinstance(player, dict) and "hand" not in player for player in players)
    assert "seed" not in offer["view"]


def test_recovery_returns_same_unsubmitted_offer_and_deadline(table: RunningTable) -> None:
    first = table.a.play(table.a.session_id)
    recovered = table.a.play(table.a.session_id)
    assert recovered == first
    assert first["status"] == "decision"
    assert first["offer"]["deadline"] is not None


def test_staging_advances_arena_while_submitter_waits(table: RunningTable) -> None:
    first = table.a.play(table.a.session_id)["offer"]
    assert first is not None
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(table.a.play, table.a.session_id, proposal_for(first))
        other_offer = table.b.play(table.b.session_id)["offer"]
        assert other_offer is not None
        assert MatchView.model_validate(other_offer["view"]).you.player_id == "b"
        assert not pending.done()
        table.stopped.set()
        table.a.stop()
        table.b.stop()
        response = pending.result(timeout=3)
    assert response["status"] == "terminal"
    assert response["receipt"] is not None
    assert response["receipt"]["status"] == "accepted"
    assert response["result"]["outcome"] == "abandoned"


def test_duplicate_during_wait_does_not_stage_another_proposal(table: RunningTable) -> None:
    first = table.a.play(table.a.session_id)["offer"]
    assert first is not None
    proposal = proposal_for(first)
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(table.a.play, table.a.session_id, proposal)
        table.b.play(table.b.session_id)
        duplicate = table.a.play(table.a.session_id, proposal)
        assert duplicate["status"] == "already_waiting"
        assert duplicate["receipt"] is not None
        assert duplicate["receipt"]["submission_id"] == proposal["submission_id"]
        table.a.stop()
        table.b.stop()
        pending.result(timeout=3)
    assert table.a.stats()["submissions"] == 1


def test_rpc_cancellation_keeps_published_move_and_allows_recovery(table: RunningTable) -> None:
    first = table.a.play(table.a.session_id)["offer"]
    assert first is not None
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
    assert recovered["receipt"] is not None
    assert recovered["receipt"]["status"] == "accepted"


def test_rejection_returns_new_offer_without_applying_memory() -> None:
    table = RunningTable(memory=True)
    try:
        first = table.a.play(table.a.session_id)["offer"]
        assert first is not None
        hand = MatchView.model_validate(first["view"]).you.hand
        missing = next(card for card in range(1, 105) if card not in hand)
        proposal = proposal_for(first, [{"type": "select_card", "card": missing}])
        proposal["memory"] = "This must not stick."
        correction = table.a.play(table.a.session_id, proposal)
        assert correction["status"] == "decision"
        assert correction["receipt"] is not None
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
        assert first is not None
        proposal = proposal_for(first)
        response = table.a.play(table.a.session_id, proposal)
        recovered = table.a.play(table.a.session_id, proposal)
        assert recovered == response
        assert recovered["status"] == "decision"
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
    assert first is not None
    proposal = proposal_for(first)
    proposal["session_id"] = table.b.session_id
    with pytest.raises(HarnessError) as error:
        table.a.play(table.a.session_id, proposal)
    assert error.value.code == "invalid_session"
    assert table.a.stats()["submissions"] == 0


def test_wait_expiry_returns_receipt_and_preserves_submission(table: RunningTable) -> None:
    table.a.wait_timeout_seconds = 0.02
    first = table.a.play(table.a.session_id)["offer"]
    assert first is not None
    proposal = proposal_for(first)
    expired = table.a.play(table.a.session_id, proposal)
    assert expired["status"] == "wait_expired"
    assert expired["receipt"] is not None
    assert expired["receipt"]["status"] == "accepted"
    recovered = table.a.play(table.a.session_id)
    assert recovered["status"] == "wait_expired"
    assert recovered["offer"] is None
    assert recovered["receipt"] is not None
    assert recovered["receipt"]["submission_id"] == proposal["submission_id"]
    assert table.a.stats()["submissions"] == 1


def test_stale_view_is_rejected_without_changing_current_offer(table: RunningTable) -> None:
    first = table.a.play(table.a.session_id)["offer"]
    assert first is not None
    proposal = proposal_for(first)
    proposal["view_id"] = "old-view"
    with pytest.raises(HarnessError) as error:
        table.a.play(table.a.session_id, proposal)
    assert error.value.code == "stale_decision"
    assert table.a.play(table.a.session_id)["offer"] == first
    assert table.a.stats()["submissions"] == 0


def test_cancelled_request_before_staging_has_no_game_effect(table: RunningTable) -> None:
    first = table.a.play(table.a.session_id)["offer"]
    assert first is not None
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
        assert first is not None
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
        assert first is not None
        proposal = proposal_for(first)
        proposal["memory"] = "Accepted note."
        response = table.a.play(table.a.session_id, proposal)
        assert response["receipt"] is not None
        assert response["receipt"]["status"] == "accepted"
        assert response["status"] == "decision"
        assert response["offer"]["memory"] == "Accepted note."
    finally:
        table.close()


def play_to_end(session: SeatSession, *, shared: bool = False) -> TerminalReply:
    play = session.play_submission if shared else session.play
    response = play(session.session_id)
    for _ in range(100):
        if response["status"] == "terminal":
            return response
        if response["status"] == "wait_expired":
            response = play(session.session_id)
            continue
        assert response["status"] == "decision"
        proposal = agent_proposal_for(response["offer"]) if shared else proposal_for(response["offer"])
        if shared:
            Draft202012Validator(session.get_play_tool(session.session_id)["parameters"]).validate({
                "proposal": proposal
            })
        response = play(session.session_id, proposal)
    pytest.fail("Harness did not finish within its decision bound")


def test_terminal_recovery_preserves_receipts_but_rejects_new_proposals() -> None:
    table = RunningTable(baseline_opponent=True)
    try:
        first = table.a.play(table.a.session_id)["offer"]
        assert first is not None
        proposal = proposal_for(first)
        table.a.play(table.a.session_id, proposal)
        terminal = play_to_end(table.a)
        retry = table.a.play(table.a.session_id, proposal)
        assert retry["status"] == "terminal"
        assert retry["result"] == terminal["result"]
        assert retry["receipt"] is not None
        assert retry["receipt"]["submission_id"] == proposal["submission_id"]
        assert retry["receipt"] is not None
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
    futures: list[Future[TerminalReply]] = []
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(play_to_end, session) for session in (table.a, table.b)]
            responses = [future.result(timeout=10) for future in futures]
        result = table.results.get(timeout=2)
        assert isinstance(result, MatchResult)
        assert result.outcome == MatchOutcome.FINISHED
        for response in responses:
            assert response["result"]["outcome"] == "finished"
            assert response["receipt"] is not None
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


@pytest.mark.parametrize("communication", [False, True])
def test_shared_play_completes_a_match_with_exact_decision_schemas(communication: bool) -> None:
    table = RunningTable(baseline_opponent=True, communication=communication)
    try:
        reply = table.a.play_submission(table.a.session_id)
        decisions = 0
        while reply["status"] == "decision":
            offer = reply["offer"]
            proposal = {**agent_proposal_for(offer), "explanation": "Choose the lowest fitting card."}
            schema = table.a.get_play_tool(table.a.session_id)["parameters"]
            Draft202012Validator(schema).validate({"proposal": proposal})
            assert (
                schema["properties"]["proposal"]["anyOf"][0]["properties"]["decision_id"]["const"]
                == offer["decision_id"]
            )
            reply = table.a.play_submission(table.a.session_id, proposal)
            assert reply["receipt"] is not None
            assert reply["receipt"]["status"] == "accepted"
            decisions += 1
        assert reply["status"] == "terminal"
        assert reply["result"]["outcome"] == "finished"
        assert decisions >= 10
    finally:
        table.close()


def test_shared_submission_retry_includes_explanation_and_does_not_reapply() -> None:
    table = RunningTable(baseline_opponent=True)
    trace: list[dict[str, Any]] = []
    table.a.set_trace(trace.append)
    try:
        reply = table.a.play_submission(table.a.session_id)
        assert reply["status"] == "decision"
        proposal = {**agent_proposal_for(reply["offer"]), "explanation": "A small card."}
        advanced = table.a.play_submission(table.a.session_id, proposal)
        recovered = table.a.play_submission(table.a.session_id, proposal)
        assert recovered == advanced
        assert table.a.stats()["submissions"] == 1
        explanations = [event for event in trace if event["type"] == "decision_explanation"]
        assert len(explanations) == 1
        assert explanations[0]["text"] == "A small card."
        with pytest.raises(HarnessError) as conflict:
            table.a.play_submission(table.a.session_id, {**proposal, "explanation": "A different reason."})
        assert conflict.value.code == "submission_conflict"
    finally:
        table.close()


def test_shared_submission_binds_view_and_rejects_stale_decision(table: RunningTable) -> None:
    reply = table.a.play_submission(table.a.session_id)
    assert reply["status"] == "decision"
    proposal = agent_proposal_for(reply["offer"])
    with pytest.raises(HarnessError) as stale:
        table.a.play_submission(table.a.session_id, {**proposal, "decision_id": "old"})
    assert stale.value.code == "stale_decision"
    assert table.a.stats()["submissions"] == 0
    with pytest.raises(HarnessError) as metadata:
        table.a.play_submission(table.a.session_id, {**proposal, "view_id": reply["offer"]["view_id"]})
    assert metadata.value.code == "invalid_proposal"


def test_shared_rejection_keeps_memory_atomic_and_returns_current_schema() -> None:
    table = RunningTable(memory=True)
    try:
        reply = table.a.play_submission(table.a.session_id)
        assert reply["status"] == "decision"
        hand = MatchView.model_validate(reply["offer"]["view"]).you.hand
        missing = next(card for card in range(1, 105) if card not in hand)
        proposal = agent_proposal_for(reply["offer"], [{"type": "select_card", "card": missing}])
        proposal["memory"] = "Must not be saved."
        correction = table.a.play_submission(table.a.session_id, proposal)
        assert correction["status"] == "decision"
        assert correction["receipt"] is not None
        assert correction["receipt"]["status"] == "rejected"
        assert correction["offer"]["memory"] is None
        assert correction["offer"]["decision_id"] != reply["offer"]["decision_id"]
        assert correction["offer"]["rejection"] is not None
    finally:
        table.close()


@pytest.mark.arena_slow
@pytest.mark.parametrize("communication", [False, True], ids=["classic", "communication"])
def test_shared_play_matches_deterministic_baselines_at_volume(communication: bool) -> None:
    for seed in range(50):
        table = RunningTable(communication=communication, seed=seed)
        try:
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(play_to_end, session, shared=True) for session in (table.a, table.b)]
                responses = [future.result(timeout=10) for future in futures]
            result = table.results.get(timeout=2)
            assert isinstance(result, MatchResult)
            assert result.outcome == MatchOutcome.FINISHED
            assert all(response["result"]["outcome"] == "finished" for response in responses)
            baseline = run_match(
                [LowestFittingCardBot(), LowestFittingCardBot()],
                seed=seed,
                match_id="harness-test",
                seats=[PlayerSeat(player_id="a"), PlayerSeat(player_id="b")],
                rules=table.rules,
                protocol=table.protocol,
            )
            assert result.final_state == baseline.final_state
        finally:
            table.close()
