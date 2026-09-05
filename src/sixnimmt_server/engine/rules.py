"""Game configuration: fixed rules of 6 nimmt! plus per-match experiment settings."""

from enum import StrEnum

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
    model_config = ConfigDict(frozen=True)

    card_selection: CardSelectionPolicy = CardSelectionPolicy.HIDDEN
    private_message_existence: PrivateMessageExistence = PrivateMessageExistence.VISIBLE


# The engine builds the game to these numbers directly: `deal` lays out ten
# cards each from a 104-card deck and turns up four rows, and resolution closes
# a row at five cards. Accepting any other value would report a ruleset the
# match does not actually play. Parameterising the engine is what would relax
# this; until then the published shape is the only shape.
PUBLISHED_SHAPE: dict[str, int] = {
    "cards_per_hand": 10,
    "row_count": 4,
    "row_capacity": 5,
    "deck_size": 104,
}


class GameRules(BaseModel):
    """The published rules of 6 nimmt!, not experimental parameters."""

    model_config = ConfigDict(frozen=True)

    min_players: int = 2
    max_players: int = 10
    cards_per_hand: int = 10
    row_count: int = 4
    row_capacity: int = 5
    deck_size: int = 104
    target_score: int = 66

    @model_validator(mode="after")
    def _check_published_shape(self) -> "GameRules":
        for field_name, published in PUBLISHED_SHAPE.items():
            supplied = getattr(self, field_name)
            if supplied != published:
                msg = f"{field_name} is fixed at {published} by the rules of 6 nimmt!, got {supplied}"
                raise ValueError(msg)
        return self

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

    model_config = ConfigDict(frozen=True)

    end_condition: EndCondition = EndCondition.TARGET_SCORE
    hands: int | None = None
    negotiation_enabled: bool = False
    information_policy: InformationPolicy = Field(default_factory=InformationPolicy)
    allow_direct_messages: bool = True
    max_actions_per_play: int | None = Field(default=None, ge=1)
    max_message_length: int = 2000
    on_invalid_action: OnInvalidAction = OnInvalidAction.REJECT
    anonymise_display_names: bool = False

    @model_validator(mode="after")
    def _check_fixed_hand_count(self) -> "MatchProtocol":
        if self.end_condition == EndCondition.FIXED_HANDS and (self.hands is None or self.hands < 1):
            msg = "fixed-hands matches require a positive hand count"
            raise ValueError(msg)
        return self
