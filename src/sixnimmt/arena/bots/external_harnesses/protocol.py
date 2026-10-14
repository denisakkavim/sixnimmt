"""Strict, transport-independent proposals; the engine still owns move legality."""

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from sixnimmt.arena.bots.agent_contract import ACTION_ADAPTER
from sixnimmt.arena.bots.agent_protocol import ActionPayload, Identifier, submission_schema
from sixnimmt.arena.bots.base import MAX_BATCH_OPERATIONS, ActionBatch, Rejection
from sixnimmt.arena.bots.external_harnesses.messages import ProtocolVersion, RejectionData
from sixnimmt.common.text import check_representable

PROTOCOL_VERSION = 1
MAX_PROPOSAL_BYTES = 131_072
MAX_EXPLANATION_CHARS = 1000


class HarnessError(Exception):
    """A safe, actionable protocol error, without credentials or raw input."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


_ENVELOPE_FIELDS = {"action_id", "from_view", "expected_view_version"}


class Proposal(_Payload):
    protocol_version: ProtocolVersion
    session_id: Identifier
    decision_id: Identifier
    submission_id: Identifier
    view_id: Identifier
    actions: list[ActionPayload] = Field(max_length=MAX_BATCH_OPERATIONS)
    memory: str | None = Field(max_length=16_000)

    @field_validator("memory")
    @classmethod
    def check_memory_text(cls, value: str | None) -> str | None:
        check_representable(value)
        return value

    @model_validator(mode="after")
    def check_operation_count(self) -> "Proposal":
        if not 1 <= len(self.actions) + int(self.memory is not None) <= MAX_BATCH_OPERATIONS:
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


def _proposal_schema(model: type[Proposal]) -> dict[str, Any]:
    schema = model.model_json_schema()
    for definition in schema.get("$defs", {}).values():
        properties = definition.get("properties", {})
        if "action_id" not in properties:
            continue
        for field in _ENVELOPE_FIELDS:
            properties.pop(field, None)
        properties["type"].pop("default", None)
        required = definition.setdefault("required", [])
        if "type" not in required:
            required.append("type")
        definition["additionalProperties"] = False
    return schema


PROPOSAL_SCHEMA: dict[str, Any] = _proposal_schema(Proposal)


class ManagedProposal(Proposal):
    """A managed command's game proposal with optional operator commentary."""

    explanation: str | None = Field(
        default=None,
        max_length=MAX_EXPLANATION_CHARS,
        description="A brief explanation of the proposed move for the operator, in one or two sentences.",
    )

    @field_validator("explanation")
    @classmethod
    def check_explanation_text(cls, value: str | None) -> str | None:
        check_representable(value)
        return value

    def game_proposal(self) -> dict[str, Any]:
        """Strip operator metadata before the broker sees the transaction."""
        return self.model_dump(mode="json", exclude={"explanation"})


MANAGED_PROPOSAL_SCHEMA: dict[str, Any] = _proposal_schema(ManagedProposal)


def decision_proposal_schema(
    offer: Mapping[str, Any], *, memory_enabled: bool, memory_max_chars: int
) -> dict[str, Any]:
    """Constrain model output to the same actions and values offered to LLM bots."""
    schema = submission_schema(
        offer["action_tools"],
        memory_enabled=memory_enabled,
        memory_max_chars=memory_max_chars,
        decision_id=offer["decision_id"],
        strict=False,
    )
    schema["properties"].pop("explanation")
    for field in ("protocol_version", "session_id", "view_id"):
        schema["properties"][field] = {**PROPOSAL_SCHEMA["properties"][field], "const": offer[field]}
        schema["required"].append(field)
    return schema


def managed_proposal_schema(offer: Mapping[str, Any], *, memory_enabled: bool, memory_max_chars: int) -> dict[str, Any]:
    """Preserve the custom-command envelope and its optional commentary."""
    schema = decision_proposal_schema(offer, memory_enabled=memory_enabled, memory_max_chars=memory_max_chars)
    schema["properties"]["explanation"] = deepcopy(MANAGED_PROPOSAL_SCHEMA["properties"]["explanation"])
    return schema


def parse_proposal(value: object) -> Proposal:
    return _parse_proposal(value, Proposal)


def parse_managed_proposal(value: object) -> ManagedProposal:
    return _parse_proposal(value, ManagedProposal)


def _parse_proposal[ProposalType: Proposal](value: object, model: type[ProposalType]) -> ProposalType:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise HarnessError("invalid_proposal", "Proposal must be representable JSON") from error
    if len(encoded) > MAX_PROPOSAL_BYTES:
        raise HarnessError("invalid_proposal", f"Proposal exceeds {MAX_PROPOSAL_BYTES} bytes")
    try:
        return model.model_validate(value)
    except ValidationError as error:
        paths = [".".join(str(part) for part in item["loc"]) for item in error.errors(include_input=False)[:4]]
        fields = ", ".join(paths)
        raise HarnessError("invalid_proposal", f"Proposal does not match the schema; check fields: {fields}") from error


def rejection_data(rejection: Rejection | None) -> RejectionData | None:
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
