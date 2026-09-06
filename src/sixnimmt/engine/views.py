"""Per-role projections of match state. A viewer sees only their own view."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from sixnimmt.engine.actions import MessageVisibility
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.engine.state import Phase


class ViewRole(StrEnum):
    PLAYER = "player"
    PUBLIC_SPECTATOR = "public_spectator"
    OMNISCIENT_OBSERVER = "omniscient_observer"
    ADMIN = "admin"


class RowView(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    cards: tuple[int, ...]


class PlayerSelfView(BaseModel):
    model_config = ConfigDict(frozen=True)

    player_id: str
    hand: tuple[int, ...]
    selection: int | None = None
    committed: bool = False
    penalty_cards: tuple[int, ...] = ()
    score_this_hand: int = 0
    total_score: int = 0
    actions_taken_this_play: int = 0
    actions_remaining_this_play: int | None = None


class OpponentView(BaseModel):
    model_config = ConfigDict(frozen=True)

    player_id: str
    display_name: str
    cards_in_hand: int
    has_selection: bool = False
    committed: bool = False
    # Always None in another player's view: knowing that someone selected
    # is public, knowing what they selected is not.
    selection: None = None
    penalty_cards: tuple[int, ...] = ()
    score_this_hand: int = 0
    total_score: int = 0


class MessageView(BaseModel):
    model_config = ConfigDict(frozen=True)
    from_player: str
    visibility: MessageVisibility
    to_player: str | None = None
    body: str


class PrivateMessageView(BaseModel):
    model_config = ConfigDict(frozen=True)
    from_player: str
    to_player: str


class RevealedCardView(BaseModel):
    """A publicly revealed move; a missing row means placement is still pending."""

    model_config = ConfigDict(frozen=True)

    player_id: str
    card: int
    row_index: int | None = None
    captured: tuple[int, ...] = ()


class PlayHistoryView(BaseModel):
    model_config = ConfigDict(frozen=True)

    hand_number: int
    play_number: int
    cards: tuple[RevealedCardView, ...]


class MessageHistoryView(BaseModel):
    model_config = ConfigDict(frozen=True)

    hand_number: int
    play_number: int
    message: MessageView | PrivateMessageView


class MatchView(BaseModel):
    """What one caller sees when reading match state."""

    model_config = ConfigDict(frozen=True)

    match_id: str
    # The caller's own gap-free position in their visible event stream.
    view_version: int
    # Opaque identifier for this exact projection. Must not encode anything
    # (global counters, timestamps) that would leak hidden activity.
    view_id: str
    status: str
    phase: Phase
    hand_number: int
    play_number: int
    you: PlayerSelfView
    rows: tuple[RowView, ...] = ()
    players: tuple[OpponentView, ...] = ()
    revealed_this_hand: tuple[tuple[int, ...], ...] = ()
    # Bounded histories retain the end of a hand when the next deal starts.
    # Both are built only from this viewer's visible events.
    play_history: tuple[PlayHistoryView, ...] = ()
    message_history: tuple[MessageHistoryView, ...] = ()
    awaiting: str | None = None
    awaiting_card: int | None = None
    # Advisory presentation hint. The action runner revalidates everything.
    legal_actions: tuple[str, ...] = ()
    target_score: int = 66
    protocol: MatchProtocol = Field(default_factory=MatchProtocol)
    messages: tuple[MessageView, ...] = ()
    private_messages_observed: tuple[PrivateMessageView, ...] = ()
    messages_omitted: int = Field(default=0, ge=0)
