"""Strict, transport-independent proposals; the engine still owns move legality."""

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Annotated, Any
from uuid import uuid4

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    ValidationError,
    field_validator,
    model_validator,
)

from sixnimmt.arena.bots.agent_contract import ACTION_ADAPTER
from sixnimmt.arena.bots.base import MAX_BATCH_OPERATIONS, ActionBatch, Rejection
from sixnimmt.arena.bots.external_harnesses.messages import ProtocolVersion, RejectionData
from sixnimmt.common.text import check_representable
from sixnimmt.engine.actions import Action

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


def _action_payload_fields() -> dict[str, frozenset[str]]:
    fields: dict[str, frozenset[str]] = {}
    for schema in ACTION_ADAPTER.json_schema()["$defs"].values():
        properties = schema.get("properties", {})
        name = properties.get("type", {}).get("const")
        if isinstance(name, str):
            fields[name] = frozenset(properties) - _ENVELOPE_FIELDS
    return fields


_ACTION_FIELDS = _action_payload_fields()


def _parse_action_payload(value: object) -> Action:
    """Keep the harness envelope strict while sharing the engine's payload rules."""
    if not isinstance(value, dict):
        msg = "game action must be an object"
        raise ValueError(msg)  # noqa: TRY004 -- Pydantic wraps ValueError as a validation failure.
    name = value.get("type")
    allowed = _ACTION_FIELDS.get(name) if isinstance(name, str) else None
    if allowed is None or len(value.keys() - allowed) > 0:
        msg = "game action has an unknown type or unsupported fields"
        raise ValueError(msg)
    return ACTION_ADAPTER.validate_json(json.dumps(value, allow_nan=False), strict=True)


def _action_payload_json(action: Action) -> dict[str, JsonValue]:
    return action.model_dump(mode="json", exclude=_ENVELOPE_FIELDS)


ActionPayload = Annotated[Action, BeforeValidator(_parse_action_payload), PlainSerializer(_action_payload_json)]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]


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
        name = properties.get("type", {}).get("const")
        if name not in _ACTION_FIELDS:
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
    schema = deepcopy(PROPOSAL_SCHEMA)
    schema.pop("$defs", None)
    alternatives: list[dict[str, Any]] = []
    for tool in offer["action_tools"]:
        function = tool["function"]
        parameters = deepcopy(function["parameters"])
        parameters["properties"] = {"type": {"type": "string", "const": function["name"]}, **parameters["properties"]}
        parameters["required"] = ["type", *parameters["required"]]
        if function["name"] == "send_message":
            alternatives.extend(_message_schemas(parameters))
        else:
            alternatives.append(parameters)
    actions = schema["properties"]["actions"]
    actions["items"] = {"anyOf": alternatives}
    if not memory_enabled:
        actions["minItems"] = 1
        schema["properties"]["memory"] = {"type": "null", "description": "Notebook updates are disabled."}
    else:
        schema["properties"]["memory"] = {
            "anyOf": [{"type": "string", "maxLength": memory_max_chars}, {"type": "null"}],
            "description": "Null preserves the notebook. An update counts as one of the eight allowed operations.",
        }
    for field in ("protocol_version", "session_id", "decision_id", "view_id"):
        schema["properties"][field]["const"] = offer[field]
    return schema


def managed_proposal_schema(offer: Mapping[str, Any], *, memory_enabled: bool, memory_max_chars: int) -> dict[str, Any]:
    """Add optional commentary without extending the native MCP proposal contract."""
    schema = decision_proposal_schema(offer, memory_enabled=memory_enabled, memory_max_chars=memory_max_chars)
    schema["properties"]["explanation"] = deepcopy(MANAGED_PROPOSAL_SCHEMA["properties"]["explanation"])
    return schema


def _message_schemas(parameters: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep table and direct recipient constraints aligned with their visibility."""
    alternatives: list[dict[str, Any]] = []
    properties = parameters["properties"]
    recipients = [recipient for recipient in properties["to_player"]["enum"] if recipient is not None]
    for visibility in properties["visibility"]["enum"]:
        message = deepcopy(parameters)
        message["properties"]["visibility"] = {"type": "string", "const": visibility}
        message["properties"]["to_player"] = (
            {"type": "null"} if visibility == "table" else {"type": "string", "enum": recipients}
        )
        if visibility == "direct" and "to_player" not in message["required"]:
            message["required"].append("to_player")
        alternatives.append(message)
    return alternatives


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
