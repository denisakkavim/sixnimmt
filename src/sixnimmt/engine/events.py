"""Game events. Each event names its audience, which alone decides who may see it."""

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, NotRequired, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator, with_config


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
    ACTION_COUNTED = "action_counted"
    ACTION_REJECTED = "action_rejected"


EVENT_TYPES: tuple[str, ...] = tuple(event_type.value for event_type in EventType)


# An event addresses one player as `player:<id>`, so the id itself may not
# contain the separator; match creation refuses ids that would be unaddressable.
AUDIENCE_SEPARATOR = ":"

# A player id travels further than any other client-supplied string: into an
# event audience, a token map key, a JSONL log encoded as UTF-8, and back out
# through replay. Rather than defend each of those in turn, ids are held to one
# bounded ASCII grammar at the door. A lone surrogate, for instance, is a legal
# JSON string that cannot be encoded to UTF-8 at all, and would otherwise fail
# somewhere deep in the first deal rather than at the request that introduced it.
PLAYER_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def audience_for_player(player_id: str) -> str:
    return f"player{AUDIENCE_SEPARATOR}{player_id}"


def assign_sequence(events: list["Event"], first_seq: int, server_action_seq: int = 0) -> list["Event"]:
    """Number a batch into the match's global sequence, newest batch last.

    The global `seq` is admin-only; players see their own gap-free cursor.
    """
    return [
        event.model_copy(update={"seq": first_seq + offset, "server_action_seq": server_action_seq})
        for offset, event in enumerate(events)
    ]


@with_config(ConfigDict(extra="forbid"))
class EmptyData(TypedDict):
    pass


@with_config(ConfigDict(extra="forbid"))
class EventPlayer(TypedDict):
    player_id: str
    display_name: str
    agent_metadata: NotRequired[dict[str, JsonValue]]


@with_config(ConfigDict(extra="forbid"))
class MatchCreatedData(TypedDict):
    players: list[EventPlayer]
    rules: dict[str, JsonValue]
    protocol: dict[str, JsonValue]


@with_config(ConfigDict(extra="forbid"))
class MatchSeedAssignedData(TypedDict):
    match_seed: int


@with_config(ConfigDict(extra="forbid"))
class HandStartedData(TypedDict):
    hand_number: int


@with_config(ConfigDict(extra="forbid"))
class HandSeedAssignedData(TypedDict):
    hand_number: int
    hand_seed: int


@with_config(ConfigDict(extra="forbid"))
class CardsDealtData(TypedDict):
    player_id: str
    hand: list[int]


@with_config(ConfigDict(extra="forbid"))
class RowsInitialisedData(TypedDict):
    rows: list[list[int]]


@with_config(ConfigDict(extra="forbid"))
class PlayStartedData(TypedDict):
    hand: int
    play: int


@with_config(ConfigDict(extra="forbid"))
class SelectionMadeData(TypedDict):
    player_id: str
    card: int


@with_config(ConfigDict(extra="forbid"))
class SelectionRegisteredData(TypedDict):
    player_id: str


@with_config(ConfigDict(extra="forbid"))
class SelectionClearedData(TypedDict):
    player_id: str


@with_config(ConfigDict(extra="forbid"))
class PlayerCommittedData(TypedDict):
    player_id: str


@with_config(ConfigDict(extra="forbid"))
class PlayerUncommittedData(TypedDict):
    player_id: str


@with_config(ConfigDict(extra="forbid"))
class CardsRevealedData(TypedDict):
    selections: dict[str, int]


@with_config(ConfigDict(extra="forbid"))
class CardPlacedData(TypedDict):
    card: int
    row: int
    row_cards: list[int]


@with_config(ConfigDict(extra="forbid"))
class RowTakenData(TypedDict):
    player_id: str
    row: int
    captured: list[int]
    heads: int
    reason: Literal["sixth_card", "too_low"]


@with_config(ConfigDict(extra="forbid"))
class RowChoiceRequiredData(TypedDict):
    player_id: str
    card: int


@with_config(ConfigDict(extra="forbid"))
class RowChoiceMadeData(TypedDict):
    player_id: str
    row: int


@with_config(ConfigDict(extra="forbid"))
class PlayEndedData(TypedDict):
    play: int
    penalties: dict[str, int]


@with_config(ConfigDict(extra="forbid"))
class HandEndedData(TypedDict):
    hand: int
    hand_scores: dict[str, int]
    totals: dict[str, int]


@with_config(ConfigDict(extra="forbid"))
class MatchEndedData(TypedDict):
    totals: dict[str, int]
    winners: list[str]


@with_config(ConfigDict(extra="forbid"))
class MatchAbandonedData(TypedDict):
    outcome: str
    ended_by: str | None
    reason: str | None


@with_config(ConfigDict(extra="forbid"))
class ActionCountedData(TypedDict):
    player_id: str
    actions_taken_this_play: int
    actions_remaining_this_play: int | None


@with_config(ConfigDict(extra="forbid"))
class ActionRejectedData(TypedDict):
    code: str
    message: str
    action_type: str


MessageSentData = TypedDict(
    "MessageSentData", {"from": str, "visibility": Literal["table", "direct"], "body": str, "to": NotRequired[str]}
)
MessageSentData = with_config(ConfigDict(extra="forbid"))(MessageSentData)
PrivateMessageOccurredData = TypedDict("PrivateMessageOccurredData", {"from": str, "to": str})
PrivateMessageOccurredData = with_config(ConfigDict(extra="forbid"))(PrivateMessageOccurredData)


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


class EventBase(EventEnvelope):
    """One concrete event type; subclasses fix the type discriminator."""

    model_config = ConfigDict(frozen=True)


class MatchCreatedEvent(EventBase):
    audience: Literal["public", "admin"]

    data: MatchCreatedData

    type: Literal[EventType.MATCH_CREATED] = EventType.MATCH_CREATED

    @model_validator(mode="after")
    def validate_public_payload(self) -> Self:
        if self.audience == "public" and any("agent_metadata" in player for player in self.data["players"]):
            msg = "public match creation must not contain agent metadata"
            raise ValueError(msg)
        return self


class MatchSeedAssignedEvent(EventBase):
    data: MatchSeedAssignedData

    type: Literal[EventType.MATCH_SEED_ASSIGNED] = EventType.MATCH_SEED_ASSIGNED


class MatchStartedEvent(EventBase):
    data: EmptyData = Field(default_factory=dict)

    type: Literal[EventType.MATCH_STARTED] = EventType.MATCH_STARTED


class HandStartedEvent(EventBase):
    data: HandStartedData

    type: Literal[EventType.HAND_STARTED] = EventType.HAND_STARTED


class HandSeedAssignedEvent(EventBase):
    data: HandSeedAssignedData

    type: Literal[EventType.HAND_SEED_ASSIGNED] = EventType.HAND_SEED_ASSIGNED


class CardsDealtEvent(EventBase):
    data: CardsDealtData

    type: Literal[EventType.CARDS_DEALT] = EventType.CARDS_DEALT


class RowsInitialisedEvent(EventBase):
    data: RowsInitialisedData

    type: Literal[EventType.ROWS_INITIALISED] = EventType.ROWS_INITIALISED


class PlayStartedEvent(EventBase):
    data: PlayStartedData

    type: Literal[EventType.PLAY_STARTED] = EventType.PLAY_STARTED


class SelectionMadeEvent(EventBase):
    data: SelectionMadeData

    type: Literal[EventType.SELECTION_MADE] = EventType.SELECTION_MADE


class SelectionRegisteredEvent(EventBase):
    data: SelectionRegisteredData

    type: Literal[EventType.SELECTION_REGISTERED] = EventType.SELECTION_REGISTERED


class SelectionClearedEvent(EventBase):
    data: SelectionClearedData

    type: Literal[EventType.SELECTION_CLEARED] = EventType.SELECTION_CLEARED


class PlayerCommittedEvent(EventBase):
    data: PlayerCommittedData

    type: Literal[EventType.PLAYER_COMMITTED] = EventType.PLAYER_COMMITTED


class PlayerUncommittedEvent(EventBase):
    data: PlayerUncommittedData

    type: Literal[EventType.PLAYER_UNCOMMITTED] = EventType.PLAYER_UNCOMMITTED


class MessageSentEvent(EventBase):
    data: MessageSentData

    type: Literal[EventType.MESSAGE_SENT] = EventType.MESSAGE_SENT


class PrivateMessageOccurredEvent(EventBase):
    data: PrivateMessageOccurredData

    type: Literal[EventType.PRIVATE_MESSAGE_OCCURRED] = EventType.PRIVATE_MESSAGE_OCCURRED


class PlayCommittedEvent(EventBase):
    data: EmptyData = Field(default_factory=dict)

    type: Literal[EventType.PLAY_COMMITTED] = EventType.PLAY_COMMITTED


class CardsRevealedEvent(EventBase):
    data: CardsRevealedData

    type: Literal[EventType.CARDS_REVEALED] = EventType.CARDS_REVEALED


class CardPlacedEvent(EventBase):
    data: CardPlacedData

    type: Literal[EventType.CARD_PLACED] = EventType.CARD_PLACED


class RowTakenEvent(EventBase):
    data: RowTakenData

    type: Literal[EventType.ROW_TAKEN] = EventType.ROW_TAKEN


class RowChoiceRequiredEvent(EventBase):
    data: RowChoiceRequiredData

    type: Literal[EventType.ROW_CHOICE_REQUIRED] = EventType.ROW_CHOICE_REQUIRED


class RowChoiceMadeEvent(EventBase):
    data: RowChoiceMadeData

    type: Literal[EventType.ROW_CHOICE_MADE] = EventType.ROW_CHOICE_MADE


class PlayEndedEvent(EventBase):
    data: PlayEndedData

    type: Literal[EventType.PLAY_ENDED] = EventType.PLAY_ENDED


class HandEndedEvent(EventBase):
    data: HandEndedData

    type: Literal[EventType.HAND_ENDED] = EventType.HAND_ENDED


class MatchEndedEvent(EventBase):
    data: MatchEndedData

    type: Literal[EventType.MATCH_ENDED] = EventType.MATCH_ENDED


class MatchAbandonedEvent(EventBase):
    data: MatchAbandonedData | EmptyData = Field(default_factory=dict)

    type: Literal[EventType.MATCH_ABANDONED] = EventType.MATCH_ABANDONED

    @model_validator(mode="after")
    def validate_audience_payload(self) -> Self:
        if self.audience != "admin" and len(self.data) > 0:
            msg = "abandonment diagnostics must be admin-only"
            raise ValueError(msg)
        if self.audience == "admin" and len(self.data) == 0:
            msg = "admin abandonment requires diagnostics"
            raise ValueError(msg)
        return self


class ActionCountedEvent(EventBase):
    data: ActionCountedData

    type: Literal[EventType.ACTION_COUNTED] = EventType.ACTION_COUNTED


class ActionRejectedEvent(EventBase):
    data: ActionRejectedData

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
    | ActionCountedEvent
    | ActionRejectedEvent,
    Field(discriminator="type"),
]
"""Every event the engine can emit, dispatched on the type field."""
