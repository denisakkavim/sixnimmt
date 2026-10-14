"""The typed atomic play contract shared by model APIs and external agents."""

import json
from collections.abc import Sequence
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

from sixnimmt.arena.bots.agent_contract import ACTION_ADAPTER, ActionTool, ToolFunction
from sixnimmt.arena.bots.base import MAX_BATCH_OPERATIONS, ActionBatch
from sixnimmt.common.text import check_representable
from sixnimmt.engine.actions import Action

MAX_SUBMISSION_BYTES = 131_072
MAX_EXPLANATION_CHARS = 1000
_ENVELOPE_FIELDS = {"action_id", "from_view", "expected_view_version"}
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]


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


class AgentSubmission(BaseModel):
    """A complete transaction, independent of transport and engine audit metadata."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    decision_id: Identifier
    submission_id: Identifier
    actions: list[ActionPayload] = Field(max_length=MAX_BATCH_OPERATIONS)
    memory: str | None = Field(max_length=16_000)
    explanation: str | None = Field(default=None, max_length=MAX_EXPLANATION_CHARS)

    @field_validator("memory", "explanation")
    @classmethod
    def check_text(cls, value: str | None) -> str | None:
        check_representable(value)
        return value

    @model_validator(mode="after")
    def check_operation_count(self) -> "AgentSubmission":
        if not 1 <= len(self.actions) + int(self.memory is not None) <= MAX_BATCH_OPERATIONS:
            msg = "A proposal must contain one to eight operations, including any memory update"
            raise ValueError(msg)
        return self

    def canonical(self) -> str:
        """Compare retries after validation and optional-field normalization."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def batch(self, view_id: str) -> ActionBatch:
        actions = tuple(
            ACTION_ADAPTER.validate_json(
                json.dumps({**action.model_dump(), "action_id": uuid4().hex, "from_view": view_id}), strict=True
            )
            for action in self.actions
        )
        return ActionBatch(actions=actions, memory=self.memory)


def parse_submission(value: object) -> AgentSubmission:
    """Reject malformed or oversized submissions without echoing private input."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        msg = "Proposal must be representable JSON"
        raise ValueError(msg) from error
    if len(encoded) > MAX_SUBMISSION_BYTES:
        msg = f"Proposal exceeds {MAX_SUBMISSION_BYTES} bytes"
        raise ValueError(msg)
    try:
        return AgentSubmission.model_validate(value)
    except ValidationError as error:
        fields = set(AgentSubmission.model_fields)
        fields.update(_ACTION_FIELDS)
        for action_fields in _ACTION_FIELDS.values():
            fields.update(action_fields)
        paths = [
            ".".join(str(part) if isinstance(part, int) or part in fields else "unknown_field" for part in item["loc"])
            for item in error.errors(include_input=False)[:4]
        ]
        msg = "Proposal does not match the schema; check fields: " + ", ".join(paths)
        raise ValueError(msg) from error


def parse_play_arguments(arguments: object) -> AgentSubmission | None:
    if not isinstance(arguments, dict) or set(arguments) != {"proposal"}:
        msg = "play requires exactly one proposal field"
        raise ValueError(msg)
    if arguments["proposal"] is None:
        return None
    return parse_submission(arguments["proposal"])


def _inline_schema(value: Any, definitions: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [_inline_schema(item, definitions) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        return _inline_schema(definitions[value["$ref"].removeprefix("#/$defs/")], definitions)
    return {key: _inline_schema(item, definitions) for key, item in value.items() if key not in ("title", "default")}


def _static_actions() -> list[dict[str, Any]]:
    definitions = ACTION_ADAPTER.json_schema()["$defs"]
    alternatives: list[dict[str, Any]] = []
    for definition in definitions.values():
        name = definition.get("properties", {}).get("type", {}).get("const")
        if name not in _ACTION_FIELDS:
            continue
        action = _inline_schema(definition, definitions)
        for field in _ENVELOPE_FIELDS:
            action["properties"].pop(field, None)
        action["required"] = ["type", *action.get("required", [])]
        action["additionalProperties"] = False
        if name == "send_message":
            alternatives.extend(_message_schemas(action))
        else:
            alternatives.append(action)
    return alternatives


def _message_schemas(parameters: dict[str, Any]) -> list[dict[str, Any]]:
    alternatives: list[dict[str, Any]] = []
    properties = parameters["properties"]
    direct_recipient: dict[str, Any] = {"type": "string"}
    if "enum" in properties["to_player"]:
        direct_recipient["enum"] = [recipient for recipient in properties["to_player"]["enum"] if recipient is not None]
    for visibility in properties["visibility"]["enum"]:
        message = deepcopy(parameters)
        message["properties"]["visibility"] = {"type": "string", "const": visibility}
        message["properties"]["to_player"] = {"type": "null"} if visibility == "table" else deepcopy(direct_recipient)
        if visibility == "direct" and "to_player" not in message["required"]:
            message["required"].append("to_player")
        alternatives.append(message)
    return alternatives


def _action_schemas(action_tools: Sequence[ActionTool] | None) -> list[dict[str, Any]]:
    if action_tools is None:
        return _static_actions()
    alternatives: list[dict[str, Any]] = []
    for tool in action_tools:
        function = tool["function"]
        parameters = deepcopy(function["parameters"])
        parameters["properties"] = {"type": {"type": "string", "const": function["name"]}, **parameters["properties"]}
        parameters["required"] = ["type", *parameters["required"]]
        if function["name"] == "send_message":
            alternatives.extend(_message_schemas(parameters))
        else:
            alternatives.append(parameters)
    return alternatives


def _require_all_properties(schema: dict[str, Any]) -> None:
    """Strict model APIs require every object property to be explicitly present."""
    if schema.get("type") == "object":
        schema["required"] = list(schema["properties"])
        schema["additionalProperties"] = False
    for value in schema.values():
        if isinstance(value, dict):
            _require_all_properties(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _require_all_properties(item)


def submission_schema(
    action_tools: Sequence[ActionTool] | None,
    *,
    memory_enabled: bool,
    memory_max_chars: int,
    decision_id: str | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Preserve the current decision's exact action variants and argument constraints."""
    schema = AgentSubmission.model_json_schema()
    schema.pop("$defs", None)
    actions = schema["properties"]["actions"]
    alternatives = _action_schemas(action_tools)
    actions["items"] = {"anyOf": alternatives} if len(alternatives) > 0 else False
    if not memory_enabled:
        actions["minItems"] = 1
        schema["properties"]["memory"] = {"type": "null", "description": "Notebook updates are disabled."}
    else:
        schema["properties"]["memory"] = {
            "anyOf": [{"type": "string", "maxLength": memory_max_chars}, {"type": "null"}],
            "description": "Null preserves the notebook. An update counts as one of the eight allowed operations.",
        }
    schema["properties"]["explanation"].pop("default", None)
    schema["properties"]["explanation"]["description"] = (
        "A brief explanation of the proposed move for the operator, in one or two sentences; null to omit."
    )
    if decision_id is not None:
        schema["properties"]["decision_id"]["const"] = decision_id
    if strict:
        _require_all_properties(schema)
    return schema


def play_tool(
    action_tools: Sequence[ActionTool] | None,
    *,
    memory_enabled: bool,
    memory_max_chars: int,
    decision_id: str | None = None,
    strict: bool = True,
) -> ActionTool:
    schema = submission_schema(
        action_tools,
        memory_enabled=memory_enabled,
        memory_max_chars=memory_max_chars,
        decision_id=decision_id,
        strict=strict,
    )
    function: ToolFunction = {
        "name": "play",
        "description": (
            "Submit one complete atomic proposal for the current decision. Use a fresh submission_id; "
            "retry identical submissions with the same ID. Null enters or recovers an external game session."
        ),
        "parameters": {
            "type": "object",
            "properties": {"proposal": {"anyOf": [schema, {"type": "null"}]}},
            "required": ["proposal"],
            "additionalProperties": False,
        },
    }
    if strict:
        function["strict"] = True
    return {"type": "function", "function": function}
