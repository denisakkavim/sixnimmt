"""Typed events: audience classes and dispatch on the type field."""

import pytest
from pydantic import JsonValue, TypeAdapter, ValidationError

from sixnimmt.engine.events import (
    EVENT_TYPES,
    Event,
    MatchCreatedData,
    MatchCreatedEvent,
    MatchStartedEvent,
    assign_sequence,
    audience_for_player,
)


def test_audience_classes_are_public_player_or_admin() -> None:
    assert audience_for_player("alice") == "player:alice"


def test_all_event_types_exist() -> None:
    expected = {
        "match_created",
        "match_seed_assigned",
        "match_started",
        "hand_started",
        "hand_seed_assigned",
        "cards_dealt",
        "rows_initialised",
        "play_started",
        "selection_made",
        "selection_registered",
        "selection_cleared",
        "player_committed",
        "player_uncommitted",
        "message_sent",
        "private_message_occurred",
        "play_committed",
        "cards_revealed",
        "card_placed",
        "row_taken",
        "row_choice_required",
        "row_choice_made",
        "play_ended",
        "hand_ended",
        "match_ended",
        "match_abandoned",
        "action_counted",
        "action_rejected",
    }

    assert set(EVENT_TYPES) == expected


def test_ambiguous_all_audience_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MatchStartedEvent(
            match_id="m_01",
            audience="all",  # type: ignore[arg-type]
            hand=1,
            play=1,
        )


def test_event_union_dispatches_on_type_discriminator() -> None:
    adapter: TypeAdapter[Event] = TypeAdapter(Event)

    event = adapter.validate_python({
        "type": "match_started",
        "match_id": "m_01",
        "audience": "public",
        "hand": 1,
        "play": 1,
    })

    assert event.type == "match_started"


def test_unknown_event_type_is_rejected() -> None:
    adapter: TypeAdapter[Event] = TypeAdapter(Event)

    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "game_won", "match_id": "m_01"})


@pytest.mark.parametrize("data", [{}, {"card": 10, "row": 0}, {"card": 10, "row": "wrong", "row_cards": [10]}])
def test_card_placement_rejects_missing_or_invalid_payload_fields(data: dict[str, JsonValue]) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Event).validate_python({"type": "card_placed", "match_id": "m", "audience": "public", "data": data})


def test_public_creation_rejects_private_agent_metadata() -> None:
    with pytest.raises(ValidationError, match="agent metadata"):
        TypeAdapter(Event).validate_python({
            "type": "match_created",
            "match_id": "m",
            "audience": "public",
            "data": {
                "rules": {},
                "protocol": {},
                "players": [{"player_id": "alice", "display_name": "Alice", "agent_metadata": {"secret": "notes"}}],
            },
        })


def test_public_abandonment_rejects_private_diagnostics() -> None:
    with pytest.raises(ValidationError, match="admin-only"):
        TypeAdapter(Event).validate_python({
            "type": "match_abandoned",
            "match_id": "m",
            "audience": "public",
            "data": {"outcome": "failed", "ended_by": "alice", "reason": "private error"},
        })


def test_event_annotations_remain_compatible_with_typed_payloads() -> None:
    event = TypeAdapter(Event).validate_python({
        "type": "card_placed",
        "match_id": "m",
        "audience": "public",
        "data": {"card": 10, "row": 0, "row_cards": [10]},
        "player_display_names": {"alice": "Alice"},
    })
    assert event.type == "card_placed"
    assert event.data == {"card": 10, "row": 0, "row_cards": [10]}


@pytest.fixture
def public_creation_event() -> MatchCreatedEvent:
    return MatchCreatedEvent(
        match_id="m",
        audience="public",
        data={"players": [{"player_id": "alice", "display_name": "Alice"}], "rules": {}, "protocol": {}},
    )


def test_mutating_a_payload_snapshot_preserves_the_public_event(public_creation_event: MatchCreatedEvent) -> None:
    before = public_creation_event.model_dump_json()
    payload = public_creation_event.data

    payload["players"][0]["agent_metadata"] = {"secret": "private diagnostics"}
    payload["players"].append({"player_id": "bob", "display_name": "Bob"})

    assert public_creation_event.model_dump_json() == before
    assert public_creation_event.data["players"] == [{"player_id": "alice", "display_name": "Alice"}]


def test_numbered_events_keep_independent_payload_snapshots(public_creation_event: MatchCreatedEvent) -> None:
    numbered = assign_sequence([public_creation_event], first_seq=7)[0]
    assert isinstance(numbered, MatchCreatedEvent)

    numbered.data["players"][0]["display_name"] = "Changed"

    assert numbered.data == public_creation_event.data
    assert numbered.data["players"][0]["display_name"] == "Alice"
    assert numbered.seq == 7
    assert public_creation_event.seq == 0


def test_event_snapshots_retain_the_wire_payload(public_creation_event: MatchCreatedEvent) -> None:
    adapter: TypeAdapter[Event] = TypeAdapter(Event)

    restored = adapter.validate_json(adapter.dump_json(public_creation_event))

    assert restored == public_creation_event
    assert restored.model_dump()["data"] == public_creation_event.data


def test_event_construction_copies_nested_input_containers() -> None:
    source: MatchCreatedData = {
        "players": [{"player_id": "alice", "display_name": "Alice"}],
        "rules": {},
        "protocol": {},
    }
    event = MatchCreatedEvent(match_id="m", audience="public", data=source)

    source["players"][0]["agent_metadata"] = {"private": "not public"}

    assert event.data["players"] == [{"player_id": "alice", "display_name": "Alice"}]
