"""Typed events: audience classes and dispatch on the type field."""

import pytest
from pydantic import TypeAdapter, ValidationError

from sixnimmt_server.engine.events import (
    EVENT_TYPES,
    Event,
    MatchCreatedEvent,
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
        "action_rejected",
    }

    assert set(EVENT_TYPES) == expected


def test_ambiguous_all_audience_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MatchCreatedEvent(
            match_id="m_01",
            audience="all",  # type: ignore[arg-type]
            hand=1,
            play=1,
        )


def test_event_union_dispatches_on_type_discriminator() -> None:
    adapter: TypeAdapter[Event] = TypeAdapter(Event)

    event = adapter.validate_python({
        "type": "match_created",
        "match_id": "m_01",
        "audience": "public",
        "hand": 1,
        "play": 1,
    })

    assert event.type == "match_created"


def test_unknown_event_type_is_rejected() -> None:
    adapter: TypeAdapter[Event] = TypeAdapter(Event)

    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "game_won", "match_id": "m_01"})
