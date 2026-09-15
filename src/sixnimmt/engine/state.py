"""Authoritative match state: players, rows, phases, and resolution progress."""

from enum import StrEnum
from typing import Unpack

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from typing_extensions import TypedDict


class Phase(StrEnum):
    SETUP = "setup"
    SELECTING = "selecting"
    RESOLVING = "resolving"
    AWAITING_ROW_CHOICE = "awaiting_row_choice"
    FINISHED = "finished"


class PlayerSeat(BaseModel):
    """Who a player is, as supplied at match creation."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    player_id: str
    display_name: str = ""
    # Opaque to the engine: never parsed, never shown to other players, always
    # written to the log so a result can be traced back to what produced it.
    agent_metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def name_or_id(self) -> str:
        return self.display_name if self.display_name != "" else self.player_id


class PlayerState(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    player_id: str
    display_name: str = ""
    agent_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    hand: tuple[int, ...] = ()
    selection: int | None = None
    committed: bool = False
    penalty_cards: tuple[int, ...] = ()
    score_this_hand: int = 0
    total_score: int = 0
    actions_taken_this_play: int = 0


class RowState(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    cards: tuple[int, ...] = Field(min_length=1)


class ResolutionState(BaseModel):
    model_config = ConfigDict(frozen=True)

    ordered_cards: tuple[tuple[int, str], ...] = ()
    next_index: int = 0
    awaiting_player: str | None = None
    scores_before_play: tuple[tuple[str, int], ...] = ()


class MatchState(BaseModel):
    model_config = ConfigDict(frozen=True)

    match_id: str
    phase: Phase = Phase.SETUP
    players: tuple[PlayerState, ...] = ()
    rows: tuple[RowState, ...] = ()
    hand_number: int = 1
    play_number: int = 1
    resolution: ResolutionState | None = None
    # Needed to deal later hands and to verify no card is lost or duplicated.
    # Never exposed in player views.
    match_seed: int | None = None
    undealt_remainder: tuple[int, ...] = ()
    revealed_this_hand: tuple[tuple[int, ...], ...] = ()


class PlayerChanges(TypedDict, total=False, closed=True):
    hand: tuple[int, ...]
    selection: int | None
    committed: bool
    penalty_cards: tuple[int, ...]
    score_this_hand: int
    total_score: int
    actions_taken_this_play: int


class MatchChanges(TypedDict, total=False, closed=True):
    phase: Phase
    players: tuple[PlayerState, ...]
    rows: tuple[RowState, ...]
    hand_number: int
    play_number: int
    resolution: ResolutionState | None
    undealt_remainder: tuple[int, ...]
    revealed_this_hand: tuple[tuple[int, ...], ...]


class ResolutionChanges(TypedDict, total=False, closed=True):
    next_index: int
    awaiting_player: str | None


def replace_player(player: PlayerState, **changes: Unpack[PlayerChanges]) -> PlayerState:
    """Copy trusted transition values with statically checked field names and types."""
    return player.model_copy(update=changes)


def replace_match(state: MatchState, **changes: Unpack[MatchChanges]) -> MatchState:
    """Keep immutable transitions cheap without accepting arbitrary update mappings."""
    return state.model_copy(update=changes)


def replace_resolution(resolution: ResolutionState, **changes: Unpack[ResolutionChanges]) -> ResolutionState:
    return resolution.model_copy(update=changes)
