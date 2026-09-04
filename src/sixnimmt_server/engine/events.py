"""Game events. Each event names its audience, which alone decides who may see it."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    MATCH_CREATED = "match_created"
    MATCH_SEED_ASSIGNED = "match_seed_assigned"
    MATCH_STARTED = "match_started"
    HAND_STARTED = "hand_started"
    HAND_SEED_ASSIGNED = "hand_seed_assigned"
    CARDS_DEALT = "cards_dealt"
    ROWS_INITIALISED = "rows_initialised"
    PLAY_STARTED = "play_started"
    SELECTION_MADE = "selection_made"
    SELECTION_REGISTERED = "selection_registered"
    SELECTION_CLEARED = "selection_cleared"
    PLAYER_COMMITTED = "player_committed"
    PLAYER_UNCOMMITTED = "player_uncommitted"
    MESSAGE_SENT = "message_sent"
    PRIVATE_MESSAGE_OCCURRED = "private_message_occurred"
    PLAY_COMMITTED = "play_committed"
    CARDS_REVEALED = "cards_revealed"
    CARD_PLACED = "card_placed"
    ROW_TAKEN = "row_taken"
    ROW_CHOICE_REQUIRED = "row_choice_required"
    ROW_CHOICE_MADE = "row_choice_made"
    PLAY_ENDED = "play_ended"
    HAND_ENDED = "hand_ended"
    MATCH_ENDED = "match_ended"
    MATCH_ABANDONED = "match_abandoned"
    ACTION_REJECTED = "action_rejected"


EVENT_TYPES: tuple[str, ...] = tuple(event_type.value for event_type in EventType)


def audience_for_player(player_id: str) -> str:
    return f"player:{player_id}"


class EventEnvelope(BaseModel):
    """Fields shared by every event."""

    model_config = ConfigDict(frozen=True)

    match_id: str
    # Global sequence over every event in the match. Never shown to players;
    # they see only their own gap-free cursor over the events visible to them.
    seq: int = 0
    server_action_seq: int = 0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    hand: int = 1
    play: int = 1
    audience: Annotated[str, Field(pattern="^(public|player:[^:]+|admin)$")]
    data: dict[str, Any] = Field(default_factory=dict)


class EventBase(EventEnvelope):
    """One concrete event type; subclasses fix the type discriminator."""

    model_config = ConfigDict(frozen=True)


class MatchCreatedEvent(EventBase):
    type: Literal[EventType.MATCH_CREATED] = EventType.MATCH_CREATED


class MatchSeedAssignedEvent(EventBase):
    type: Literal[EventType.MATCH_SEED_ASSIGNED] = EventType.MATCH_SEED_ASSIGNED


class MatchStartedEvent(EventBase):
    type: Literal[EventType.MATCH_STARTED] = EventType.MATCH_STARTED


class HandStartedEvent(EventBase):
    type: Literal[EventType.HAND_STARTED] = EventType.HAND_STARTED


class HandSeedAssignedEvent(EventBase):
    type: Literal[EventType.HAND_SEED_ASSIGNED] = EventType.HAND_SEED_ASSIGNED


class CardsDealtEvent(EventBase):
    type: Literal[EventType.CARDS_DEALT] = EventType.CARDS_DEALT


class RowsInitialisedEvent(EventBase):
    type: Literal[EventType.ROWS_INITIALISED] = EventType.ROWS_INITIALISED


class PlayStartedEvent(EventBase):
    type: Literal[EventType.PLAY_STARTED] = EventType.PLAY_STARTED


class SelectionMadeEvent(EventBase):
    type: Literal[EventType.SELECTION_MADE] = EventType.SELECTION_MADE


class SelectionRegisteredEvent(EventBase):
    type: Literal[EventType.SELECTION_REGISTERED] = EventType.SELECTION_REGISTERED


class SelectionClearedEvent(EventBase):
    type: Literal[EventType.SELECTION_CLEARED] = EventType.SELECTION_CLEARED


class PlayerCommittedEvent(EventBase):
    type: Literal[EventType.PLAYER_COMMITTED] = EventType.PLAYER_COMMITTED


class PlayerUncommittedEvent(EventBase):
    type: Literal[EventType.PLAYER_UNCOMMITTED] = EventType.PLAYER_UNCOMMITTED


class MessageSentEvent(EventBase):
    type: Literal[EventType.MESSAGE_SENT] = EventType.MESSAGE_SENT


class PrivateMessageOccurredEvent(EventBase):
    type: Literal[EventType.PRIVATE_MESSAGE_OCCURRED] = EventType.PRIVATE_MESSAGE_OCCURRED


class PlayCommittedEvent(EventBase):
    type: Literal[EventType.PLAY_COMMITTED] = EventType.PLAY_COMMITTED


class CardsRevealedEvent(EventBase):
    type: Literal[EventType.CARDS_REVEALED] = EventType.CARDS_REVEALED


class CardPlacedEvent(EventBase):
    type: Literal[EventType.CARD_PLACED] = EventType.CARD_PLACED


class RowTakenEvent(EventBase):
    type: Literal[EventType.ROW_TAKEN] = EventType.ROW_TAKEN


class RowChoiceRequiredEvent(EventBase):
    type: Literal[EventType.ROW_CHOICE_REQUIRED] = EventType.ROW_CHOICE_REQUIRED


class RowChoiceMadeEvent(EventBase):
    type: Literal[EventType.ROW_CHOICE_MADE] = EventType.ROW_CHOICE_MADE


class PlayEndedEvent(EventBase):
    type: Literal[EventType.PLAY_ENDED] = EventType.PLAY_ENDED


class HandEndedEvent(EventBase):
    type: Literal[EventType.HAND_ENDED] = EventType.HAND_ENDED


class MatchEndedEvent(EventBase):
    type: Literal[EventType.MATCH_ENDED] = EventType.MATCH_ENDED


class MatchAbandonedEvent(EventBase):
    type: Literal[EventType.MATCH_ABANDONED] = EventType.MATCH_ABANDONED


class ActionRejectedEvent(EventBase):
    type: Literal[EventType.ACTION_REJECTED] = EventType.ACTION_REJECTED


Event = Annotated[
    MatchCreatedEvent
    | MatchSeedAssignedEvent
    | MatchStartedEvent
    | HandStartedEvent
    | HandSeedAssignedEvent
    | CardsDealtEvent
    | RowsInitialisedEvent
    | PlayStartedEvent
    | SelectionMadeEvent
    | SelectionRegisteredEvent
    | SelectionClearedEvent
    | PlayerCommittedEvent
    | PlayerUncommittedEvent
    | MessageSentEvent
    | PrivateMessageOccurredEvent
    | PlayCommittedEvent
    | CardsRevealedEvent
    | CardPlacedEvent
    | RowTakenEvent
    | RowChoiceRequiredEvent
    | RowChoiceMadeEvent
    | PlayEndedEvent
    | HandEndedEvent
    | MatchEndedEvent
    | MatchAbandonedEvent
    | ActionRejectedEvent,
    Field(discriminator="type"),
]
"""Every event the engine can emit, dispatched on the type field."""
