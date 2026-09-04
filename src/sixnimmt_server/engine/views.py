"""Role-filtered view shapes (§9.2, §5.4)."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from sixnimmt_server.engine.state import Phase


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
    # Always None in player views under the hidden policy (§9.2).
    selection: None = None
    penalty_cards: tuple[int, ...] = ()
    score_this_hand: int = 0
    total_score: int = 0


class MatchView(BaseModel):
    """The caller's view from GET /matches/{id}/state (§9.2)."""

    model_config = ConfigDict(frozen=True)

    match_id: str
    # The caller's own gap-free cursor, never a global counter (§10.2).
    view_version: int
    # Opaque identifier for this exact projection (§9.2).
    view_id: str
    status: str
    phase: Phase
    hand_number: int
    play_number: int
    you: PlayerSelfView
    rows: tuple[RowView, ...] = ()
    players: tuple[OpponentView, ...] = ()
    revealed_this_hand: tuple[tuple[int, ...], ...] = ()
    awaiting: str | None = None
    # Advisory presentation information; the server revalidates (§9.2).
    legal_actions: tuple[str, ...] = ()
    target_score: int = 66
