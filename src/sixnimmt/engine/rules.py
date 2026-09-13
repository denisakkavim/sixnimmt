"""Game configuration: fixed rules of 6 nimmt! plus per-match experiment settings."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CardSelectionPolicy(StrEnum):
    HIDDEN = "hidden"


class PrivateMessageExistence(StrEnum):
    VISIBLE = "visible"
    HIDDEN = "hidden"


class EndCondition(StrEnum):
    TARGET_SCORE = "target_score"
    FIXED_HANDS = "fixed_hands"


class OnInvalidAction(StrEnum):
    REJECT = "reject"


class InformationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    card_selection: CardSelectionPolicy = CardSelectionPolicy.HIDDEN
    private_message_existence: PrivateMessageExistence = PrivateMessageExistence.VISIBLE


class GameRules(BaseModel):
    """The published rules of 6 nimmt!, not experimental parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_players: int = 2
    max_players: int = 10
    cards_per_hand: Literal[10] = 10
    row_count: Literal[4] = 4
    row_capacity: Literal[5] = 5
    deck_size: Literal[104] = 104
    target_score: int = Field(default=66, gt=0)

    @model_validator(mode="after")
    def _check_player_bounds(self) -> "GameRules":
        if self.min_players < 2:
            msg = "min_players must be at least 2"
            raise ValueError(msg)
        if self.max_players > 10:
            msg = "max_players must be at most 10"
            raise ValueError(msg)
        if self.min_players > self.max_players:
            msg = "min_players must not exceed max_players"
            raise ValueError(msg)
        return self


class MatchProtocol(BaseModel):
    """Protocol-level overrides for controlled comparisons."""

    # A stale mode flag must not silently fall back to classic play.
    model_config = ConfigDict(frozen=True, extra="forbid")

    end_condition: EndCondition = EndCondition.TARGET_SCORE
    hands: int | None = None
    communication_enabled: bool = False
    information_policy: InformationPolicy = Field(default_factory=InformationPolicy)
    allow_direct_messages: bool = True
    max_actions_per_play: int | None = Field(default=None, ge=1)
    max_message_length: int = Field(default=2000, ge=0)
    on_invalid_action: OnInvalidAction = OnInvalidAction.REJECT
    anonymise_display_names: bool = False

    @model_validator(mode="after")
    def _check_fixed_hand_count(self) -> "MatchProtocol":
        if self.end_condition == EndCondition.FIXED_HANDS and (self.hands is None or self.hands < 1):
            msg = "fixed-hands matches require a positive hand count"
            raise ValueError(msg)
        return self


class _RecordedRules(GameRules):
    # Historical values are facts about a recorded match, not new configuration.
    target_score: int = 66


class _RecordedProtocol(MatchProtocol):
    max_message_length: int = 2000


def rules_from_recording(value: object) -> GameRules:
    """Read older rules without applying new-input bounds or rejecting old keys."""
    recorded = _RecordedRules.model_validate(value, extra="ignore")
    # Historical validation establishes the fields; preserve the public model type
    # without reapplying bounds that did not exist when the log was written.
    return GameRules.model_construct(**{name: getattr(recorded, name) for name in GameRules.model_fields})


def protocol_from_recording(value: object) -> MatchProtocol:
    """Keep historical configuration decoding separate from experiment input."""
    recorded = _RecordedProtocol.model_validate(value, extra="ignore")
    return MatchProtocol.model_construct(**{name: getattr(recorded, name) for name in MatchProtocol.model_fields})
