"""One bounded model decision per arena offer, with no privileged game access."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from openai import APIError, OpenAI
from openai.types.chat import ChatCompletion
from pydantic import Field, JsonValue, ValidationError, field_validator

from sixnimmt_server.arena.bots.base import Bot, BotOptions, Rejection
from sixnimmt_server.arena.bots.prompt import ACTION_ADAPTER, PROMPT_VERSION, SYSTEM_PROMPT, action_tools
from sixnimmt_server.engine.actions import Action
from sixnimmt_server.engine.cards import bull_heads
from sixnimmt_server.engine.views import MatchView


class LLMOptions(BotOptions):
    model: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key_env: str | None = Field(default=None, min_length=1)
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    max_tokens: int = Field(default=2048, ge=1)
    token_limit_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"  # noqa: S105 - API parameter name
    request_timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    decision_budget_seconds: float = Field(default=120.0, gt=0, allow_inf_nan=False)
    repair_attempts: int = Field(default=1, ge=0, le=3)
    tool_choice: Literal["required", "auto"] | None = "required"
    disable_parallel_tool_calls: bool = True
    strict_tools: bool = False
    system_prompt: str = Field(default=SYSTEM_PROMPT, min_length=1)
    strategy_prompt: str = ""
    provider_options: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("provider_options")
    @classmethod
    def validate_provider_options(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        reserved = {
            "model",
            "messages",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "stream",
            "n",
            "max_tokens",
            "max_completion_tokens",
            "temperature",
            "api_key",
            "authorization",
        }
        if reserved.intersection(value):
            msg = "provider_options must not override game requests, standard options, or credentials"
            raise ValueError(msg)
        return value

    @field_validator("base_url")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or parsed.hostname is None:
            msg = "base_url must be an absolute HTTP(S) API URL"
            raise ValueError(msg)
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            msg = "base_url must not contain credentials, query parameters, or a fragment"
            raise ValueError(msg)
        return value


class ModelDecisionError(RuntimeError):
    """The model endpoint did not supply a usable action."""


def parse_action(response: Any, view: MatchView) -> Action:
    if not response.choices:
        msg = "response has no choices"
        raise ValueError(msg)
    calls = response.choices[0].message.tool_calls or []
    if len(calls) != 1 or calls[0].type != "function":
        msg = "return exactly one function tool call"
        raise ValueError(msg)
    call = calls[0].function
    if call.name not in view.legal_actions:
        msg = "call one of the available tools"
        raise ValueError(msg)
    arguments = json.loads(call.arguments)
    if not isinstance(arguments, dict):
        msg = "tool arguments must be a JSON object"
        raise TypeError(msg)
    allowed = next(
        tool["function"]["parameters"]["properties"]
        for tool in action_tools(view, False)
        if tool["function"]["name"] == call.name
    )
    if arguments.keys() - allowed.keys():
        msg = "tool arguments contain unsupported fields"
        raise ValueError(msg)
    return ACTION_ADAPTER.validate_json(
        json.dumps({**arguments, "type": call.name, "from_view": view.view_id}), strict=True
    )


class LLMBot(Bot):
    def __init__(self, seed: int, **options: Any) -> None:
        self.options = LLMOptions.model_validate(options)
        self._api_key = "unused"
        if self.options.api_key_env is not None:
            key = os.environ.get(self.options.api_key_env)
            if key is None or key == "":
                msg = "the configured API key environment variable is empty or missing"
                raise ValueError(msg)
            self._api_key = key
        self._trace: Callable[[dict[str, Any]], None] | None = None
        self._last_action: dict[str, Any] | None = None
        self._stats: dict[str, Any] = {
            "calls": 0,
            "repairs": 0,
            "errors": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "usage_missing_calls": 0,
            "request_duration_ms": 0.0,
            "model": self.options.model,
            "response_models": [],
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": hashlib.sha256(self._instructions().encode()).hexdigest(),
        }

    def set_trace(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        """Attach privileged instrumentation without changing game actions."""
        self._trace = callback

    def _record(self, context: dict[str, Any], kind: str, **data: Any) -> None:
        if self._trace is None:
            return
        record = {**context, "kind": kind, "timestamp": datetime.now(UTC).isoformat(), **data}
        # Redact the configured credential even if a provider echoes it in a body.
        encoded = json.dumps(record)
        if self.options.api_key_env is not None:
            encoded = encoded.replace(json.dumps(self._api_key)[1:-1], "[REDACTED]")
        self._trace(json.loads(encoded))

    def _instructions(self) -> str:
        return self.options.system_prompt + "\nStrategy and personality:\n" + self.options.strategy_prompt

    def stats(self) -> dict[str, Any]:
        return {**self._stats, "response_models": list(self._stats["response_models"])}

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        observation = {
            "available_cards": list(view.you.hand),
            "view": view.model_dump(mode="json"),
            "row_penalties": {str(row.index): sum(bull_heads(card) for card in row.cards) for row in view.rows},
            "previous_action": self._last_action,
            "rejection": asdict(rejection) if rejection is not None else None,
        }
        messages = [
            {"role": "system", "content": self._instructions()},
            {"role": "user", "content": json.dumps(observation)},
        ]
        decision_id = str(uuid4())
        deadline = monotonic() + self.options.decision_budget_seconds
        for attempt in range(self.options.repair_attempts + 1):
            remaining = deadline - monotonic()
            if remaining <= 0:
                msg = "model decision budget exhausted"
                raise ModelDecisionError(msg)
            context = {
                "trace_version": 1,
                "match_id": view.match_id,
                "view_id": view.view_id,
                "view_version": view.view_version,
                "hand": view.hand_number,
                "play": view.play_number,
                "decision_id": decision_id,
                "request_id": str(uuid4()),
                "attempt": attempt,
                "prompt_version": PROMPT_VERSION,
                "prompt_sha256": self._stats["prompt_sha256"],
            }
            response = self._request(messages, view, min(remaining, self.options.request_timeout_seconds), context)
            try:
                action = parse_action(response, view)
            except (AttributeError, TypeError, ValueError, ValidationError) as error:
                self._record(context, "parse_error", error_type=type(error).__name__, message=str(error))
                self._stats["errors"] += 1
                if attempt == self.options.repair_attempts:
                    msg = "model returned an invalid tool call after bounded repair"
                    raise ModelDecisionError(msg) from None
                self._stats["repairs"] += 1
                # Keep repair context bounded and avoid echoing arbitrary provider content.
                messages.append({
                    "role": "user",
                    "content": f"Your response was invalid: {error}. Call one available tool.",
                })
                continue
            self._record(context, "action_parsed", action=action.model_dump(mode="json"))
            if monotonic() > deadline:
                self._record(context, "decision_expired")
                msg = "model decision budget exhausted"
                raise ModelDecisionError(msg)
            self._last_action = action.model_dump(mode="json")
            return action
        msg = "model decision produced no action"
        raise ModelDecisionError(msg)

    def _request(self, messages: list[dict[str, Any]], view: MatchView, timeout: float, context: dict[str, Any]) -> Any:
        parameters: dict[str, Any] = {
            "model": self.options.model,
            "messages": messages,
            "tools": action_tools(view, self.options.strict_tools),
            self.options.token_limit_parameter: self.options.max_tokens,
        }
        if self.options.temperature is not None:
            parameters["temperature"] = self.options.temperature
        if self.options.tool_choice is not None:
            parameters["tool_choice"] = self.options.tool_choice
        if self.options.disable_parallel_tool_calls:
            parameters["parallel_tool_calls"] = False
        self._record(
            context,
            "request",
            endpoint=self.options.base_url,
            timeout_seconds=timeout,
            payload={**parameters, **self.options.provider_options},
        )
        parameters["extra_body"] = self.options.provider_options
        started = monotonic()
        self._stats["calls"] += 1
        try:
            # Closing per request also releases resources after a late arena timeout.
            with OpenAI(
                base_url=self.options.base_url, api_key=self._api_key, timeout=timeout, max_retries=0
            ) as client:
                raw = client.chat.completions.with_raw_response.create(**parameters)
                self._record(
                    context,
                    "response",
                    status_code=raw.status_code,
                    provider_request_id=raw.headers.get("x-request-id"),
                    body=raw.text,
                    duration_ms=(monotonic() - started) * 1000,
                )
                try:
                    response = ChatCompletion.model_validate_json(raw.text)
                except Exception as error:
                    self._record(context, "response_parse_error", error_type=type(error).__name__, message=str(error))
                    msg = "model response could not be decoded"
                    raise ModelDecisionError(msg) from None
        except APIError as error:
            http_response = getattr(error, "response", None)
            self._record(
                context,
                "provider_error",
                error_type=type(error).__name__,
                message=str(error),
                status_code=getattr(error, "status_code", None),
                body=http_response.text if http_response is not None else None,
                provider_request_id=getattr(error, "request_id", None),
                duration_ms=(monotonic() - started) * 1000,
            )
            self._stats["errors"] += 1
            # Provider exception bodies may contain request data or authentication details.
            msg = f"model request failed ({type(error).__name__})"
            raise ModelDecisionError(msg) from None
        finally:
            self._stats["request_duration_ms"] += (monotonic() - started) * 1000
        if response.model not in self._stats["response_models"]:
            self._stats["response_models"].append(response.model)
        if response.usage is None:
            self._stats["usage_missing_calls"] += 1
        else:
            self._stats["prompt_tokens"] += response.usage.prompt_tokens
            self._stats["completion_tokens"] += response.usage.completion_tokens
        return response
