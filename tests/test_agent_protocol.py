"""Every transport gets the same typed transaction and decision constraints."""

import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.protocols import Validator

from sixnimmt.arena.bots.agent_contract import action_tools
from sixnimmt.arena.bots.agent_protocol import parse_play_arguments, parse_submission, play_tool, submission_schema
from sixnimmt.engine.audience import Viewer
from sixnimmt.engine.fold import build_view
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.views import MatchView, ViewRole


@pytest.fixture
def view() -> MatchView:
    _, events = create_match("protocol-test", ["a", "b"], 123)
    return build_view(events, Viewer(ViewRole.PLAYER, "a"))


def submission(actions: list[dict[str, Any]], **changes: Any) -> dict[str, Any]:
    return {
        "decision_id": "decision",
        "submission_id": "submission",
        "actions": actions,
        "memory": None,
        "explanation": None,
        **changes,
    }


def validator(view: MatchView, *, memory_enabled: bool = False, strict: bool = True) -> Validator:
    tool = play_tool(
        action_tools(view, strict),
        memory_enabled=memory_enabled,
        memory_max_chars=10,
        decision_id="decision",
        strict=strict,
    )
    schema = tool["function"]["parameters"]
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_hand_enum_changes_inside_the_shared_play_tool(view: MatchView) -> None:
    card = view.you.hand[0]
    move = {"proposal": submission([{"type": "select_card", "card": card}])}
    assert validator(view).is_valid(move)
    updated = view.model_copy(update={"you": view.you.model_copy(update={"hand": view.you.hand[1:]})})
    assert not validator(updated).is_valid(move)
    assert validator(view).is_valid(move)


def test_row_decision_removes_card_actions_and_constrains_row_indices(view: MatchView) -> None:
    choosing = view.model_copy(update={"legal_actions": ("choose_row",)})
    schema = validator(choosing)
    assert schema.is_valid({"proposal": submission([{"type": "choose_row", "row_index": 0}])})
    assert not schema.is_valid({"proposal": submission([{"type": "choose_row", "row_index": 4}])})
    assert not schema.is_valid({"proposal": submission([{"type": "select_card", "card": view.you.hand[0]}])})


@pytest.mark.parametrize("strict", [False, True])
def test_message_variants_keep_recipient_and_body_constraints(view: MatchView, strict: bool) -> None:
    communicating = view.model_copy(
        update={
            "protocol": MatchProtocol(communication_enabled=True, max_message_length=10),
            "legal_actions": ("select_card", "send_message"),
        }
    )
    schema = validator(communicating, strict=strict)
    action = {"type": "send_message", "visibility": "direct", "to_player": "b", "body": "hello"}
    assert schema.is_valid({"proposal": submission([action])})
    for change in ({"to_player": None}, {"to_player": "missing"}, {"visibility": "table"}, {"body": "x" * 11}):
        assert not schema.is_valid({"proposal": submission([{**action, **change}])})
    action.update(visibility="table", to_player=None)
    assert schema.is_valid({"proposal": submission([action])})


def test_play_schema_rejects_extra_fields_wrong_types_and_wrong_decision(view: MatchView) -> None:
    schema = validator(view)
    valid = submission([{"type": "select_card", "card": view.you.hand[0]}])
    for changed in (
        {**valid, "extra": "ignored"},
        {**valid, "decision_id": "previous-decision"},
        {**valid, "actions": [{"type": "select_card", "card": True}]},
        {**valid, "actions": [{"type": "select_card", "card": view.you.hand[0], "from_view": "invented"}]},
    ):
        assert not schema.is_valid({"proposal": changed})
    assert not schema.is_valid({"proposal": valid, "session_id": "invented"})


def test_memory_schema_enforces_seat_configuration(view: MatchView) -> None:
    proposal = submission([], memory="notes")
    assert validator(view, memory_enabled=True).is_valid({"proposal": proposal})
    assert not validator(view, memory_enabled=False).is_valid({"proposal": proposal})
    proposal["memory"] = "x" * 11
    assert not validator(view, memory_enabled=True).is_valid({"proposal": proposal})


@pytest.mark.parametrize("strict", [False, True])
def test_static_play_schema_is_fully_typed_before_a_decision(strict: bool) -> None:
    schema = submission_schema(None, memory_enabled=False, memory_max_chars=4000, strict=strict)
    Draft202012Validator.check_schema(schema)
    check = Draft202012Validator(schema)
    assert check.is_valid(submission([{"type": "select_card", "card": 1}]))
    assert not check.is_valid(submission([{"type": "select_card", "card": "1"}]))
    assert not check.is_valid(submission([{"type": "unknown"}]))
    assert not check.is_valid(submission([{"type": "select_card", "card": 1, "extra": False}]))
    assert not check.is_valid(
        submission([{"type": "send_message", "visibility": "direct", "body": "hello", "to_player": None}])
    )
    assert not check.is_valid(
        submission([{"type": "send_message", "visibility": "table", "body": "hello", "to_player": "b"}])
    )
    assert "$ref" not in json.dumps(schema)


def test_optional_explanation_is_normalized_for_identical_retries() -> None:
    data = submission([{"type": "select_card", "card": 1}])
    parsed = parse_submission(data)
    del data["explanation"]
    assert parsed.canonical() == parse_submission(data).canonical()


def test_explanation_is_operator_metadata_and_never_becomes_a_game_action() -> None:
    parsed = parse_submission(submission([{"type": "select_card", "card": 1}], explanation="Keep the high card."))
    batch = parsed.batch("view-1")
    assert len(batch.actions) == 1
    assert batch.actions[0].from_view == "view-1"
    assert "explanation" not in batch.actions[0].model_dump()
    assert batch.memory is None


@pytest.mark.parametrize(
    "count, memory, accepted", [(0, None, False), (0, "", True), (8, None, True), (8, "notes", False), (9, None, False)]
)
def test_operation_limit_includes_memory(count: int, memory: str | None, accepted: bool) -> None:
    data = submission([{"type": "commit"}] * count, memory=memory)
    if accepted:
        assert len(parse_submission(data).actions) == count
    else:
        with pytest.raises(ValueError, match="schema"):
            parse_submission(data)


@pytest.mark.parametrize("value", [float("nan"), "\ud800", {"private": "x" * 131_072}])
def test_submission_errors_are_bounded_and_do_not_repeat_private_input(value: object) -> None:
    with pytest.raises(ValueError) as caught:
        parse_submission(value)
    assert len(str(caught.value)) < 300
    assert "private" not in str(caught.value)


def test_null_proposal_enters_or_recovers_a_session() -> None:
    assert parse_play_arguments({"proposal": None}) is None


def test_validation_errors_do_not_echo_unknown_field_names() -> None:
    proposal = submission([{"type": "select_card", "card": 1}])
    proposal["secret-" * 1000] = "private value"
    with pytest.raises(ValueError) as caught:
        parse_submission(proposal)
    assert "secret" not in str(caught.value)
    assert "private value" not in str(caught.value)
    assert len(str(caught.value)) < 300
