"""Authoritative match state: players, rows, phases, and resolution progress."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Phase(StrEnum):
    SETUP = "setup"
    SELECTING = "selecting"
    RESOLVING = "resolving"
    AWAITING_ROW_CHOICE = "awaiting_row_choice"
    FINISHED = "finished"


class PlayerState(BaseModel):
    model_config = ConfigDict(frozen=True)

    player_id: str
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
