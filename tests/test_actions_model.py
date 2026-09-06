"""Typed actions: envelope fields and dispatch on the type field."""

from typing import Literal

import pytest
from pydantic import TypeAdapter, ValidationError

from sixnimmt.engine.actions import (
    Action,
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
        ({"type": "select_card", "card": 62}, "select_card"),
        ({"type": "commit"}, "commit"),
        ({"type": "uncommit"}, "uncommit"),
        (
            {"type": "send_message", "visibility": "table", "body": "hi"},
            "send_message",
        ),
        ({"type": "choose_row", "row_index": 0}, "choose_row"),
    ],
)
def test_action_union_dispatches_on_type_discriminator(payload: dict, expected_type: Literal["select_card"]) -> None:
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
