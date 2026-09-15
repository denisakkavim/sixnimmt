"""Strict, transport-independent proposals; the engine still owns move legality."""

import json
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from sixnimmt.arena.bots.agent_contract import ACTION_ADAPTER
from sixnimmt.arena.bots.base import ActionBatch, Rejection
from sixnimmt.common.text import check_representable

PROTOCOL_VERSION = 1
MAX_PROPOSAL_BYTES = 131_072


class HarnessError(Exception):
    """A safe, actionable protocol error, without credentials or raw input."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class _SelectCard(_Payload):
    type: Literal["select_card"]
    card: int = Field(ge=1, le=104)


class _Commit(_Payload):
    type: Literal["commit"]


class _Uncommit(_Payload):
    type: Literal["uncommit"]


class _ChooseRow(_Payload):
    type: Literal["choose_row"]
    row_index: int


class _SendMessage(_Payload):
    type: Literal["send_message"]
    visibility: Literal["table", "direct"]
    body: str
    to_player: str | None = None

    @field_validator("body", "to_player")
    @classmethod
    def check_text(cls, value: str | None) -> str | None:
        check_representable(value)
        return value

    @model_validator(mode="after")
    def check_recipient(self) -> "_SendMessage":
        if self.visibility == "direct" and self.to_player is None:
            msg = "Direct messages require a recipient"
            raise ValueError(msg)
        if self.visibility == "table" and self.to_player is not None:
            msg = "Table messages cannot name a recipient"
            raise ValueError(msg)
        return self


ActionPayload = Annotated[_SelectCard | _Commit | _Uncommit | _ChooseRow | _SendMessage, Field(discriminator="type")]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]


class Proposal(_Payload):
    protocol_version: Literal[1]
    session_id: Identifier
    decision_id: Identifier
    submission_id: Identifier
    view_id: Identifier
    actions: list[ActionPayload] = Field(max_length=8)
    memory: str | None = Field(max_length=16_000)

    @field_validator("protocol_version", mode="before")
    @classmethod
    def check_version_type(cls, value: object) -> object:
        if type(value) is not int:
            msg = "protocol_version must be an integer"
            raise ValueError(msg)
        return value

    @field_validator("memory")
    @classmethod
    def check_memory_text(cls, value: str | None) -> str | None:
        check_representable(value)
        return value

    @model_validator(mode="after")
    def check_operation_count(self) -> "Proposal":
        if not 1 <= len(self.actions) + int(self.memory is not None) <= 8:
            msg = "A proposal must contain one to eight operations, including any memory update"
            raise ValueError(msg)
        return self

    def canonical(self) -> str:
        """Compare retry content after validation and optional-field normalization."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def batch(self) -> ActionBatch:
        actions = tuple(
            ACTION_ADAPTER.validate_json(
                json.dumps({**action.model_dump(), "action_id": uuid4().hex, "from_view": self.view_id}), strict=True
            )
            for action in self.actions
        )
        return ActionBatch(actions=actions, memory=self.memory)


PROPOSAL_SCHEMA: dict[str, Any] = Proposal.model_json_schema()


def parse_proposal(value: object) -> Proposal:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise HarnessError("invalid_proposal", "Proposal must be representable JSON") from error
    if len(encoded) > MAX_PROPOSAL_BYTES:
        raise HarnessError("invalid_proposal", f"Proposal exceeds {MAX_PROPOSAL_BYTES} bytes")
    try:
        return Proposal.model_validate(value)
    except ValidationError as error:
        paths = [".".join(str(part) for part in item["loc"]) for item in error.errors(include_input=False)[:4]]
        fields = ", ".join(paths)
        raise HarnessError("invalid_proposal", f"Proposal does not match the schema; check fields: {fields}") from error


def rejection_data(rejection: Rejection | None) -> dict[str, Any] | None:
    if rejection is None:
        return None
    action = None
    if rejection.action is not None:
        action = rejection.action.model_dump(
            mode="json", exclude={"action_id", "from_view", "expected_view_version"}, exclude_none=True
        )
    return {
        "code": rejection.code.value,
        "message": rejection.message,
        "legal_actions": list(rejection.legal_actions),
        "action": action,
    }
