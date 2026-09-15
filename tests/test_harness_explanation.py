"""Optional managed commentary remains outside the authoritative game transaction."""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from sixnimmt.arena.bots.agent_contract import action_tools
from sixnimmt.arena.bots.external import ManagedHarnessBot
from sixnimmt.arena.bots.external_harnesses.broker import SeatSession
from sixnimmt.arena.bots.external_harnesses.drivers import (
    CommandProfile,
    ManagedCommandDriver,
    structured_proposal_schema,
)
from sixnimmt.arena.bots.external_harnesses.managed import ManagedSeatWorker
from sixnimmt.arena.bots.external_harnesses.messages import DecisionOffer
from sixnimmt.arena.bots.external_harnesses.protocol import (
    MAX_EXPLANATION_CHARS,
    PROPOSAL_SCHEMA,
    HarnessError,
    managed_proposal_schema,
    parse_managed_proposal,
    parse_proposal,
)
from sixnimmt.arena.bots.heuristics import LowestCardBot
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.match import run_match
from sixnimmt.arena.results import MatchOutcome, MatchResult
from sixnimmt.engine.audience import Viewer
from sixnimmt.engine.fold import build_view
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.state import PlayerSeat
from sixnimmt.engine.views import ViewRole


@pytest.fixture
def proposal() -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "session_id": "session",
        "decision_id": "decision",
        "submission_id": "submission",
        "view_id": "view",
        "actions": [{"type": "select_card", "card": 7}],
        "memory": None,
    }


@pytest.mark.parametrize("fields", [{}, {"explanation": None}, {"explanation": ""}])
def test_existing_commands_can_omit_or_decline_an_explanation(proposal: dict[str, Any], fields: dict[str, Any]) -> None:
    parsed = parse_managed_proposal({**proposal, **fields})
    assert parsed.game_proposal() == proposal
    assert parse_proposal(parsed.game_proposal()).batch().size == 1


@pytest.mark.parametrize("explanation", [123, True, ["reason"], "x" * (MAX_EXPLANATION_CHARS + 1)])
def test_explanation_type_and_character_limit_are_validated(proposal: dict[str, Any], explanation: Any) -> None:
    with pytest.raises(HarnessError, match="explanation"):
        parse_managed_proposal({**proposal, "explanation": explanation})


def test_explanation_rejects_unrepresentable_text(proposal: dict[str, Any]) -> None:
    with pytest.raises(HarnessError, match="representable JSON"):
        parse_managed_proposal({**proposal, "explanation": "\ud800"})


def test_explanation_at_the_limit_is_metadata_not_an_additional_operation(proposal: dict[str, Any]) -> None:
    explanation = "x" * MAX_EXPLANATION_CHARS
    proposal.update(actions=[{"type": "commit"}] * 7, memory="remember")
    parsed = parse_managed_proposal({**proposal, "explanation": explanation})
    assert parsed.explanation == explanation
    assert parsed.game_proposal() == proposal
    assert parsed.batch().size == 8


def test_explanation_alone_cannot_satisfy_the_game_operation_requirement(proposal: dict[str, Any]) -> None:
    proposal.update(actions=[], explanation="I am considering the board.")
    with pytest.raises(HarnessError):
        parse_managed_proposal(proposal)


def test_native_proposal_schema_and_parser_do_not_accept_managed_commentary(proposal: dict[str, Any]) -> None:
    assert "explanation" not in PROPOSAL_SCHEMA["properties"]
    assert parse_proposal(proposal).batch().size == 1
    with pytest.raises(HarnessError, match="explanation"):
        parse_proposal({**proposal, "explanation": "I choose a low card."})
    session = SeatSession("a", "A", GameRules(), MatchProtocol())
    assert "explanation" not in session.get_game_info(session.session_id)["proposal_schema"]["properties"]


def test_managed_schema_adds_nullable_explanation_without_extending_native_fields() -> None:
    _, events = create_match("explanation-schema", ["a", "b"], 123)
    view = build_view(events, Viewer(ViewRole.PLAYER, "a"))
    offer = {
        "protocol_version": 1,
        "session_id": "session",
        "decision_id": "decision",
        "view_id": view.view_id,
        "action_tools": action_tools(view, strict=False),
    }
    schema = managed_proposal_schema(offer, memory_enabled=False, memory_max_chars=4000)
    assert "explanation" not in schema["required"]
    strict = structured_proposal_schema(schema)
    Draft202012Validator.check_schema(strict)
    assert "explanation" in strict["required"]
    assert strict["properties"]["explanation"]["anyOf"] == [
        {"maxLength": MAX_EXPLANATION_CHARS, "type": "string"},
        {"type": "null"},
    ]
    assert "explanation" not in PROPOSAL_SCHEMA["properties"]


def run_managed_table(
    tmp_path: Path,
    fields: dict[str, Any],
    *,
    first_fields: dict[str, Any] | None = None,
    action_limit: int = 1,
) -> tuple[MatchResult, list[dict[str, Any]], ManagedCommandDriver]:
    source = (
        "import json, sys, uuid\n"
        "from sixnimmt.arena.bots.heuristics import LowestCardBot\n"
        "from sixnimmt.engine.views import MatchView\n"
        "request = json.load(sys.stdin)\n"
        "offer = request['offer']\n"
        "assert 'explanation' in request['game_info']['proposal_schema']['properties']\n"
        "assert 'one or two concise sentences' in offer['instructions']\n"
        "assert offer['memory'] is None\n"
        "view = MatchView.model_validate(offer['view'])\n"
        "action = LowestCardBot().act(view)\n"
        "proposal = {key: offer[key] for key in ('protocol_version', 'session_id', 'decision_id', 'view_id')}\n"
        "proposal.update(submission_id=uuid.uuid4().hex, memory=None, actions=[action.model_dump(mode='json', "
        "exclude={'action_id', 'from_view', 'expected_view_version'})])\n"
        f"fields = {fields!r}\n"
        f"first_fields = {first_fields!r}\n"
        "if first_fields is not None and 'protocol_error' not in offer:\n"
        "    fields = first_fields\n"
        "if 'protocol_error' in offer:\n"
        "    previous = offer['protocol_error']['previous_proposal']\n"
        "    assert previous['explanation'] == first_fields['explanation']\n"
        "    assert previous['decision_id'] == offer['decision_id']\n"
        "    assert previous['view_id'] == offer['view_id']\n"
        "proposal.update(fields)\n"
        "print(json.dumps(proposal))\n"
    )
    script = tmp_path / "agent.py"
    script.write_text(source, encoding="utf-8")
    rules = GameRules()
    protocol = MatchProtocol(end_condition="fixed_hands", hands=1)
    session = SeatSession("a", "A", rules, protocol)
    driver = ManagedCommandDriver(CommandProfile((sys.executable, str(script)), timeout_seconds=5), tmp_path / "work")
    bot = ManagedHarnessBot(session, ManagedSeatWorker(session, driver))
    records: list[dict[str, Any]] = []
    result = run_match(
        [bot, LowestCardBot()],
        seed=123,
        match_id="explanation",
        seats=[PlayerSeat(player_id="a"), PlayerSeat(player_id="b")],
        rules=rules,
        protocol=protocol,
        config=RunConfig(match_action_limit=action_limit, trace_dir=tmp_path / "traces", decision_timeout_seconds=5),
        on_activity=records.append,
    )
    return result, records, driver


@pytest.mark.parametrize("fields", [{}, {"explanation": None}, {"explanation": " "}])
def test_omitted_or_empty_explanation_produces_no_operator_text_or_extra_calls(
    tmp_path: Path, fields: dict[str, Any]
) -> None:
    result, records, driver = run_managed_table(tmp_path, fields)
    assert result.actions_accepted == 1
    assert driver.invocations == 1
    assert all(record["type"] != "decision_explanation" for record in records)


def test_explanation_reaches_operator_and_private_trace_before_submission_completes(tmp_path: Path) -> None:
    reason = "My low card leaves useful gaps for later plays."
    result, records, driver = run_managed_table(tmp_path, {"explanation": reason})
    assert result.actions_accepted == 1
    assert driver.invocations == 1
    record = next(record for record in records if record["type"] == "decision_explanation")
    proposal_record = next(record for record in records if record["type"] == "proposal")
    for field in ("decision_id", "view_id", "submission_id"):
        assert record[field] == proposal_record["proposal"][field]
    assert record["text"] == reason
    assert record["attempt"] == record["invocation"] == 1
    types = [record["type"] for record in records]
    assert types.index("decision_explanation") < types.index("decision_finished")
    saved = [json.loads(line) for line in (tmp_path / "traces" / "explanation.model.jsonl").read_text().splitlines()]
    assert any(item["type"] == "decision_explanation" and item["text"] == reason for item in saved)
    assert any(item["type"] == "proposal" and item["proposal"].get("explanation") == reason for item in saved)
    assert reason not in (tmp_path / "traces" / "explanation.jsonl").read_text()
    assert reason not in (tmp_path / "traces" / "explanation.actions.jsonl").read_text()


@pytest.mark.parametrize("invalid", [42, "x" * (MAX_EXPLANATION_CHARS + 1)])
def test_invalid_explanation_gets_one_repair_with_the_original_offer_and_deadline(tmp_path: Path, invalid: Any) -> None:
    result, records, driver = run_managed_table(
        tmp_path, {"explanation": "This card fits the current rows."}, first_fields={"explanation": invalid}
    )
    assert result.actions_accepted == 1
    assert driver.invocations == 2
    requests = [record for record in records if record["type"] == "decision_request"]
    assert len(requests) == 2
    for field in ("decision_id", "view_id", "deadline"):
        assert requests[0]["offer"][field] == requests[1]["offer"][field]
    assert requests[0]["proposal_schema"] == requests[1]["proposal_schema"]
    deadlines = [record["deadline"] for record in records if record["type"] == "invocation_started"]
    assert len(deadlines) == 2
    first_deadline = datetime.fromisoformat(deadlines[0])
    repair_deadline = datetime.fromisoformat(deadlines[1])
    assert abs((first_deadline - repair_deadline).total_seconds()) < 0.1
    errors = [
        record
        for record in records
        if record["type"] == "proposal_rejected" and record["error"]["code"] == "invalid_proposal"
    ]
    assert len(errors) == 1
    assert errors[0]["error"]["previous_proposal"]["explanation"] == invalid
    assert "explanation" in errors[0]["error"]["message"]
    explanations = [record for record in records if record["type"] == "decision_explanation"]
    assert len(explanations) == 1
    assert explanations[0]["attempt"] == 2


def test_invalid_explanation_on_both_attempts_fails_without_submitting_a_move(tmp_path: Path) -> None:
    result, records, driver = run_managed_table(tmp_path, {"explanation": 42}, first_fields={"explanation": 42})
    assert result.outcome == MatchOutcome.FAILED
    assert result.actions_accepted == 0
    assert driver.invocations == 2
    rejected = [record for record in records if record["type"] == "proposal_rejected"]
    assert len(rejected) == 2
    assert rejected[0]["repair_will_follow"] is True
    assert rejected[1]["repair_will_follow"] is False
    assert all(record["type"] != "decision_explanation" for record in records)


def test_delivery_cancellation_is_not_reported_as_proposal_rejection(tmp_path: Path, proposal: dict[str, Any]) -> None:
    _, events = create_match("explanation-cancel", ["a", "b"], 123)
    view = build_view(events, Viewer(ViewRole.PLAYER, "a"))
    session = SeatSession("a", "A", GameRules(), MatchProtocol())
    proposal.update(session_id=session.session_id, view_id=view.view_id, explanation="I choose the low card.")
    offer: DecisionOffer = {
        "protocol_version": 1,
        "session_id": session.session_id,
        "decision_id": proposal["decision_id"],
        "view_id": view.view_id,
        "view": view.model_dump(mode="json"),
        "observation": "",
        "instructions": "",
        "action_tools": action_tools(view, strict=False),
        "rejection": None,
        "memory": None,
        "deadline": None,
    }
    command = (sys.executable, "-c", f"print({json.dumps(proposal)!r})")
    driver = ManagedCommandDriver(CommandProfile(command), tmp_path / "work")
    records: list[dict[str, Any]] = []
    driver.set_trace(records.append)
    worker = ManagedSeatWorker(session, driver)
    worker.stopped.set()
    with pytest.raises(HarnessError, match="delivery wait was cancelled"):
        worker._submit_decision(offer, session.get_game_info(session.session_id))
    assert driver.invocations == 1
    assert all(record["type"] not in ("proposal_rejected", "protocol_repair") for record in records)
    cancelled = [record for record in records if record["type"] == "delivery_cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["decision_id"] == proposal["decision_id"]
    assert cancelled[0]["view_id"] == proposal["view_id"]
    assert cancelled[0]["error"]["previous_proposal"] == proposal


def test_explanations_preserve_an_entire_hand_and_do_not_add_model_calls(tmp_path: Path) -> None:
    result, records, driver = run_managed_table(
        tmp_path, {"explanation": "I choose the lowest legal card."}, action_limit=100
    )
    baseline = run_match(
        [LowestCardBot(), LowestCardBot()],
        seed=123,
        match_id="explanation",
        seats=[PlayerSeat(player_id="a"), PlayerSeat(player_id="b")],
        protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
    )
    assert result.outcome == baseline.outcome == MatchOutcome.FINISHED
    assert result.final_state == baseline.final_state
    assert [(event.type, event.data) for event in result.events] == [
        (event.type, event.data) for event in baseline.events
    ]
    assert result.actions_accepted == baseline.actions_accepted
    assert driver.invocations == result.seat_actions[0][0]
    assert len([record for record in records if record["type"] == "decision_explanation"]) == driver.invocations
