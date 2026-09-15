"""Managed models see schemas and repair feedback for their current decision."""

import copy
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.protocols import Validator

from sixnimmt.arena.bots.agent_contract import SYSTEM_PROMPT, action_tools, system_instructions
from sixnimmt.arena.bots.external import ManagedHarnessBot
from sixnimmt.arena.bots.external_harnesses.broker import SeatSession
from sixnimmt.arena.bots.external_harnesses.drivers import (
    CommandProfile,
    ManagedCommandDriver,
    structured_proposal_schema,
)
from sixnimmt.arena.bots.external_harnesses.managed import ManagedSeatWorker
from sixnimmt.arena.bots.external_harnesses.protocol import PROPOSAL_SCHEMA, decision_proposal_schema
from sixnimmt.arena.bots.heuristics import LowestFittingCardBot
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.match import run_match
from sixnimmt.arena.results import MatchOutcome, MatchResult
from sixnimmt.engine.audience import Viewer
from sixnimmt.engine.fold import build_view
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.state import PlayerSeat
from sixnimmt.engine.views import MatchView, ViewRole


def make_view(*, communication: bool = False, direct: bool = False) -> MatchView:
    protocol = MatchProtocol(communication_enabled=communication, allow_direct_messages=direct, max_message_length=12)
    _, events = create_match("offer-schema", ["a", "b", "c"], 123, protocol=protocol)
    return build_view(events, Viewer(ViewRole.PLAYER, "a"))


def make_offer(view: MatchView) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "session_id": "session",
        "decision_id": "decision",
        "view_id": view.view_id,
        "action_tools": action_tools(view, strict=False),
    }


def make_proposal(view: MatchView, actions: list[dict[str, Any]], memory: str | None = None) -> dict[str, Any]:
    offer = make_offer(view)
    return {
        **{key: offer[key] for key in ("protocol_version", "session_id", "decision_id", "view_id")},
        "submission_id": "submission",
        "actions": actions,
        "memory": memory,
    }


def validator_for(view: MatchView, *, memory: bool = False, memory_max_chars: int = 12) -> Validator:
    schema = decision_proposal_schema(make_offer(view), memory_enabled=memory, memory_max_chars=memory_max_chars)
    # Exercise the same normalization used by both vendor CLI profiles.
    schema = structured_proposal_schema(schema)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize("action_type", ["commit", "uncommit", "choose_row", "send_message"])
def test_classic_schema_rejects_actions_unavailable_in_the_current_offer(action_type: str) -> None:
    view = make_view()
    validator = validator_for(view)
    valid = make_proposal(view, [{"type": "select_card", "card": view.you.hand[0]}])
    assert validator.is_valid(valid)
    assert not validator.is_valid(make_proposal(view, [{"type": action_type}]))


def test_selection_schema_rejects_cards_outside_the_offered_hand() -> None:
    view = make_view()
    unavailable = next(card for card in range(1, 105) if card not in view.you.hand)
    assert not validator_for(view).is_valid(make_proposal(view, [{"type": "select_card", "card": unavailable}]))


def test_row_choice_schema_only_accepts_the_offered_rows() -> None:
    view = make_view().model_copy(update={"legal_actions": ("choose_row",)})
    validator = validator_for(view)
    assert validator.is_valid(make_proposal(view, [{"type": "choose_row", "row_index": 0}]))
    assert not validator.is_valid(make_proposal(view, [{"type": "choose_row", "row_index": 4}]))
    assert not validator.is_valid(make_proposal(view, [{"type": "select_card", "card": view.you.hand[0]}]))


def test_communication_schema_permits_commit_after_selection_in_the_same_proposal() -> None:
    view = make_view(communication=True)
    assert "commit" not in view.legal_actions
    proposal = make_proposal(view, [{"type": "select_card", "card": view.you.hand[0]}, {"type": "commit"}])
    assert validator_for(view).is_valid(proposal)


@pytest.mark.parametrize(
    ("direct", "visibility", "recipient", "body", "valid"),
    [
        (False, "table", None, "hello", True),
        (False, "direct", "b", "hello", False),
        (True, "direct", "b", "hello", True),
        (True, "direct", "a", "hello", False),
        (True, "direct", None, "hello", False),
        (True, "table", "b", "hello", False),
        (True, "table", None, "x" * 13, False),
    ],
)
def test_message_schema_enforces_permissions_recipients_and_length(
    direct: bool, visibility: str, recipient: str | None, body: str, valid: bool
) -> None:
    view = make_view(communication=True, direct=direct)
    proposal = make_proposal(
        view, [{"type": "send_message", "visibility": visibility, "to_player": recipient, "body": body}]
    )
    assert validator_for(view).is_valid(proposal) == valid


@pytest.mark.parametrize(
    ("enabled", "memory", "valid"), [(False, "", False), (True, "x" * 12, True), (True, "x" * 13, False)]
)
def test_memory_schema_respects_this_seats_configuration(enabled: bool, memory: str, valid: bool) -> None:
    view = make_view()
    proposal = make_proposal(view, [{"type": "select_card", "card": view.you.hand[0]}], memory)
    assert validator_for(view, memory=enabled).is_valid(proposal) == valid


def test_memory_disabled_schema_rejects_an_empty_transaction() -> None:
    view = make_view()
    assert not validator_for(view).is_valid(make_proposal(view, []))
    assert validator_for(view, memory=True).is_valid(make_proposal(view, [], "remember"))


@pytest.mark.parametrize("field", ["session_id", "decision_id", "view_id"])
def test_schema_requires_the_current_offer_identifiers(field: str) -> None:
    view = make_view()
    proposal = make_proposal(view, [{"type": "select_card", "card": view.you.hand[0]}])
    proposal[field] = "stale"
    assert not validator_for(view).is_valid(proposal)


def test_decision_schema_does_not_change_global_validation_or_the_offer() -> None:
    view = make_view()
    original_schema = copy.deepcopy(PROPOSAL_SCHEMA)
    offer = make_offer(view)
    original_offer = copy.deepcopy(offer)
    schema = decision_proposal_schema(offer, memory_enabled=False, memory_max_chars=12)
    assert schema != PROPOSAL_SCHEMA
    assert original_schema == PROPOSAL_SCHEMA
    assert offer == original_offer


@pytest.mark.parametrize("communication", [False, True])
def test_prompt_only_suggests_selection_then_commit_in_communication_mode(communication: bool) -> None:
    view = make_view(communication=communication)
    instructions = system_instructions(view, SYSTEM_PROMPT, "")
    assert ("select a card then commit" in instructions) == communication
    assert ("without a separate commit action" in instructions) != communication


def run_repair_table(tmp_path: Path, *, repair: bool) -> tuple[list[dict[str, Any]], MatchResult]:
    records: list[dict[str, Any]] = []
    script = tmp_path / "agent.py"
    marker = tmp_path / "previous-request.json"
    script.write_text(
        "import json, pathlib, sys, uuid\n"
        "request = json.load(sys.stdin)\n"
        "offer = request['offer']\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "if not marker.exists():\n"
        "    marker.write_text(json.dumps(request))\n"
        "else:\n"
        "    previous = json.loads(marker.read_text())\n"
        "    assert request['game_info']['proposal_schema'] == previous['game_info']['proposal_schema']\n"
        "    assert offer['view_id'] == previous['offer']['view_id']\n"
        "    assert offer['decision_id'] == previous['offer']['decision_id']\n"
        "    assert offer['deadline'] == previous['offer']['deadline']\n"
        "    assert offer['protocol_error']['previous_proposal']['actions'] == [{'type': 'commit'}]\n"
        "    assert 'commit' in offer['protocol_error']['message']\n"
        "    assert 'select_card' in offer['protocol_error']['message']\n"
        "proposal = {key: offer[key] for key in ('protocol_version', 'session_id', 'decision_id', 'view_id')}\n"
        "actions = [{'type': 'commit'}]\n"
        f"if {repair!r} and 'protocol_error' in offer:\n"
        "    actions = [{'type': 'select_card', 'card': min(offer['view']['you']['hand'])}]\n"
        "proposal.update(submission_id=uuid.uuid4().hex, actions=actions, memory=None)\n"
        "print(json.dumps(proposal))\n",
        encoding="utf-8",
    )
    rules = GameRules()
    protocol = MatchProtocol(end_condition="fixed_hands", hands=1)
    session = SeatSession("a", "A", rules, protocol)
    driver = ManagedCommandDriver(
        CommandProfile((sys.executable, str(script)), timeout_seconds=5), tmp_path / "workspace"
    )
    bot = ManagedHarnessBot(session, ManagedSeatWorker(session, driver))
    result = run_match(
        [bot, LowestFittingCardBot()],
        seed=123,
        seats=[PlayerSeat(player_id="a"), PlayerSeat(player_id="b")],
        rules=rules,
        protocol=protocol,
        config=RunConfig(match_action_limit=1, decision_timeout_seconds=5),
        on_activity=records.append,
    )
    return records, result


def test_managed_repair_receives_its_rejected_proposal_and_preserves_the_offers_schema(tmp_path: Path) -> None:
    records, result = run_repair_table(tmp_path, repair=True)
    assert result.outcome == MatchOutcome.ABANDONED
    assert result.reason == "match_action_limit"
    requests = [record for record in records if record["type"] == "decision_request"]
    assert len(requests) == 2
    validator = Draft202012Validator(requests[0]["proposal_schema"])
    proposals = [record["proposal"] for record in records if record["type"] == "proposal"]
    assert len(proposals) == 2
    assert not validator.is_valid(proposals[0])
    assert validator.is_valid(proposals[1])
    rejected = next(record for record in records if record["type"] == "proposal_rejected")
    assert rejected["error"]["code"] == "unavailable_action"
    assert rejected["repair_will_follow"] is True
    assert len([record for record in records if record["type"] == "protocol_repair"]) == 1


def test_managed_final_failure_retains_both_rejected_proposals(tmp_path: Path) -> None:
    records, result = run_repair_table(tmp_path, repair=False)
    assert result.outcome == MatchOutcome.FAILED
    rejected = [record for record in records if record["type"] == "proposal_rejected"]
    assert len(rejected) == 2
    assert rejected[1]["repair_will_follow"] is False
    assert rejected[1]["error"]["previous_proposal"]["actions"] == [{"type": "commit"}]
    assert "Unavailable action types: commit" in rejected[1]["error"]["message"]
    assert rejected[0]["decision_id"] == rejected[1]["decision_id"]
