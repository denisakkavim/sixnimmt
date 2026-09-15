"""Typed actions: envelope fields and dispatch on the type field."""

import pytest
from pydantic import JsonValue, TypeAdapter, ValidationError

from sixnimmt.engine.actions import (
    Action,
    ActionType,
    ChooseRowAction,
    CommitAction,
    MessageVisibility,
    SelectCardAction,
    SendMessageAction,
    UncommitAction,
)


def test_all_action_types_exist() -> None:
    assert Action.__doc__ is not None  # discriminated union exists
    type_names = {
        SelectCardAction.__name__,
        CommitAction.__name__,
        UncommitAction.__name__,
        SendMessageAction.__name__,
        ChooseRowAction.__name__,
    }

    assert type_names == {
        "SelectCardAction",
        "CommitAction",
        "UncommitAction",
        "SendMessageAction",
        "ChooseRowAction",
    }


def test_common_envelope_fields_are_optional_audit_metadata() -> None:
    action = SelectCardAction(card=62)

    assert action.action_id is None
    assert action.from_view is None
    assert action.expected_view_version is None


def test_direct_message_requires_recipient() -> None:
    with pytest.raises(ValidationError):
        SendMessageAction(visibility=MessageVisibility.DIRECT, body="hello")


def test_table_message_rejects_recipient() -> None:
    with pytest.raises(ValidationError):
        SendMessageAction(visibility=MessageVisibility.TABLE, body="hello", to_player="bob")


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        ({"type": "select_card", "card": 62}, ActionType.SELECT_CARD),
        ({"type": "commit"}, ActionType.COMMIT),
        ({"type": "uncommit"}, ActionType.UNCOMMIT),
        (
            {"type": "send_message", "visibility": "table", "body": "hi"},
            ActionType.SEND_MESSAGE,
        ),
        ({"type": "choose_row", "row_index": 0}, ActionType.CHOOSE_ROW),
    ],
)
def test_action_union_dispatches_on_type_discriminator(
    payload: dict[str, JsonValue], expected_type: ActionType
) -> None:
    adapter: TypeAdapter[Action] = TypeAdapter(Action)

    action = adapter.validate_python(payload)

    assert action.type == expected_type


def test_unknown_action_type_is_rejected() -> None:
    adapter: TypeAdapter[Action] = TypeAdapter(Action)

    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "fold"})


def test_actions_round_trip_through_json() -> None:
    adapter: TypeAdapter[Action] = TypeAdapter(Action)
    action = SelectCardAction(action_id="b3f1", card=62, from_view="v_9f2c", expected_view_version=87)

    restored = adapter.validate_json(adapter.dump_json(action))

    assert restored == action


@pytest.mark.parametrize("value", [True, "2", 2.0])
@pytest.mark.parametrize(
    ("action_type", "field_name"),
    [
        ("select_card", "card"),
        ("choose_row", "row_index"),
        ("commit", "expected_view_version"),
    ],
)
def test_action_integer_fields_reject_scalar_coercion(action_type: str, field_name: str, value: JsonValue) -> None:
    adapter: TypeAdapter[Action] = TypeAdapter(Action)

    with pytest.raises(ValidationError):
        adapter.validate_python({"type": action_type, field_name: value})


@pytest.mark.parametrize("row_index", [-1, 4])
def test_row_indices_remain_unbounded_until_the_engine_checks_them(row_index: int) -> None:
    action = ChooseRowAction(row_index=row_index)

    assert action.row_index == row_index
