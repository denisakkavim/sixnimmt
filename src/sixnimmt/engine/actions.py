"""Player actions: card selection, commitment, messaging, and row choice."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sixnimmt.common.text import check_representable


class ActionType(StrEnum):
    SELECT_CARD = "select_card"
    COMMIT = "commit"
    UNCOMMIT = "uncommit"
    SEND_MESSAGE = "send_message"
    CHOOSE_ROW = "choose_row"


class MessageVisibility(StrEnum):
    TABLE = "table"
    DIRECT = "direct"


class ActionEnvelope(BaseModel):
    """Shared metadata carried on every action."""

    model_config = ConfigDict(frozen=True)

    # Caller UUID for idempotency; None means the action runner assigns one.
    action_id: str | None = None
    # Audit reference only: records which observation the client acted on.
    # Never validated for freshness; a stale value never causes rejection.
    from_view: str | None = None
    # Optional optimistic concurrency, expressed in the caller's own cursor.
    expected_view_version: int | None = None


class SelectCardAction(ActionEnvelope):
    type: Literal[ActionType.SELECT_CARD] = ActionType.SELECT_CARD
    card: int = Field(ge=1, le=104)


class CommitAction(ActionEnvelope):
    type: Literal[ActionType.COMMIT] = ActionType.COMMIT


class UncommitAction(ActionEnvelope):
    type: Literal[ActionType.UNCOMMIT] = ActionType.UNCOMMIT


class SendMessageAction(ActionEnvelope):
    type: Literal[ActionType.SEND_MESSAGE] = ActionType.SEND_MESSAGE
    visibility: MessageVisibility
    body: str
    to_player: str | None = None

    @field_validator("body", "to_player")
    @classmethod
    def _check_text(cls, value: str | None) -> str | None:
        check_representable(value)
        return value

    @model_validator(mode="after")
    def _check_recipient(self) -> "SendMessageAction":
        if self.visibility == MessageVisibility.DIRECT and self.to_player is None:
            msg = "direct messages require to_player"
            raise ValueError(msg)
        if self.visibility == MessageVisibility.TABLE and self.to_player is not None:
            msg = "table messages must not name a recipient"
            raise ValueError(msg)
        return self


class ChooseRowAction(ActionEnvelope):
    type: Literal[ActionType.CHOOSE_ROW] = ActionType.CHOOSE_ROW
    # Unbounded here on purpose: which rows exist is a rule the engine owns and
    # the configuration can change, so it rejects a bad index rather than the
    # action model refusing to parse one.
    row_index: int


Action = Annotated[
    SelectCardAction | CommitAction | UncommitAction | SendMessageAction | ChooseRowAction,
    Field(discriminator="type"),
]
"""Every supported action, dispatched on the type field."""
