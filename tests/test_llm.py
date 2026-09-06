"""Model bots use tool calls through configurable endpoints without privileged state."""

import json
from typing import Any

import httpx2 as httpx
import pytest
from openai import OpenAI
from pydantic import ValidationError

from sixnimmt_server.arena.bots import GreedyBot
from sixnimmt_server.arena.bots.llm import LLMBot, LLMOptions, ModelDecisionError
from sixnimmt_server.arena.players import PlayerConfig
from sixnimmt_server.arena.runner import RunConfig, run_arena, run_match
from sixnimmt_server.engine.actions import SelectCardAction
from sixnimmt_server.engine.audience import Viewer
from sixnimmt_server.engine.fold import build_view
from sixnimmt_server.engine.replay import replay_events
from sixnimmt_server.engine.rules import MatchProtocol
from sixnimmt_server.engine.setup import create_match
from sixnimmt_server.engine.views import MatchView, ViewRole


@pytest.fixture
def view() -> MatchView:
    _, events = create_match("model-test", ["a", "b"], 123)
    return build_view(events, Viewer(ViewRole.PLAYER, "a"))


class Endpoint:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.urls: list[str] = []
        self.authorizations: list[str] = []
        self.invalid_responses = 0
        self.illegal_card_once = False
        self.status = 200

    def handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        self.urls.append(str(request.url))
        self.authorizations.append(request.headers["authorization"])
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "secret-provider-body"}})
        view = MatchView.model_validate(json.loads(payload["messages"][1]["content"])["view"])
        action = GreedyBot().act(view).model_dump(mode="json")
        name = action.pop("type")
        arguments = {k: v for k, v in action.items() if k not in ("action_id", "from_view", "expected_view_version")}
        if self.illegal_card_once:
            arguments = {"card": next(card for card in range(1, 105) if card not in view.you.hand)}
            self.illegal_card_once = False
        calls = [{"id": "call_1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]
        if self.invalid_responses > 0:
            self.invalid_responses -= 1
            calls = []
        return httpx.Response(
            200,
            json={
                "id": "completion",
                "object": "chat.completion",
                "created": 0,
                "model": "local-model-revision",
                "choices": [
                    {"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": calls}}
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            },
        )


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch) -> Endpoint:
    endpoint = Endpoint()

    # Exercise the real SDK serialization and error handling, replacing only HTTP.
    def client(**kwargs: Any) -> OpenAI:
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(endpoint.handle)))

    monkeypatch.setattr("sixnimmt_server.arena.bots.llm.OpenAI", client)
    return endpoint


def test_calls_configured_endpoint_with_only_available_tools(endpoint: Endpoint, view: MatchView) -> None:
    bot = LLMBot(1, model="my-local-model", base_url="http://localhost:11434/v1")
    action = bot.act(view)
    assert isinstance(action, SelectCardAction)
    assert action.card in view.you.hand
    assert action.from_view == view.view_id
    assert endpoint.urls == ["http://localhost:11434/v1/chat/completions"]
    payload = endpoint.requests[0]
    assert payload["model"] == "my-local-model"
    assert [tool["function"]["name"] for tool in payload["tools"]] == ["select_card"]
    assert "temperature" not in payload
    assert payload["parallel_tool_calls"] is False
    observation = json.loads(payload["messages"][1]["content"])
    assert observation["view"] == view.model_dump(mode="json")
    assert "hand" not in observation["view"]["players"][0]


def test_repairs_missing_tool_call_once(endpoint: Endpoint, view: MatchView) -> None:
    endpoint.invalid_responses = 1
    bot = LLMBot(1, model="local", base_url="http://localhost:11434/v1")
    bot.act(view)
    assert len(endpoint.requests) == 2
    assert "invalid" in endpoint.requests[1]["messages"][-1]["content"]
    assert bot.stats()["repairs"] == 1
    assert bot.stats()["prompt_tokens"] == 200


def test_exhausted_repairs_fail_without_fallback(endpoint: Endpoint, view: MatchView) -> None:
    endpoint.invalid_responses = 10
    bot = LLMBot(1, model="local", base_url="http://localhost:11434/v1")
    with pytest.raises(ModelDecisionError, match="bounded repair"):
        bot.act(view)
    assert len(endpoint.requests) == 2


def test_provider_errors_are_sanitized_and_not_retried(endpoint: Endpoint, view: MatchView) -> None:
    endpoint.status = 401
    bot = LLMBot(1, model="local", base_url="http://localhost:11434/v1")
    with pytest.raises(ModelDecisionError, match="AuthenticationError") as caught:
        bot.act(view)
    assert "secret-provider-body" not in str(caught.value)
    assert len(endpoint.requests) == 1


def test_provider_compatibility_flags_can_be_omitted(endpoint: Endpoint, view: MatchView) -> None:
    bot = LLMBot(
        1,
        model="local",
        base_url="http://localhost:11434/v1",
        tool_choice=None,
        disable_parallel_tool_calls=False,
        token_limit_parameter="max_completion_tokens",  # noqa: S106 - API field
        strict_tools=True,
    )
    bot.act(view)
    payload = endpoint.requests[0]
    assert "tool_choice" not in payload
    assert "parallel_tool_calls" not in payload
    assert payload["max_completion_tokens"] == 2048
    assert payload["tools"][0]["function"]["strict"] is True


@pytest.mark.parametrize("protocol", [MatchProtocol(), MatchProtocol(negotiation_enabled=True)])
def test_model_matches_finish_and_replay(endpoint: Endpoint, protocol: MatchProtocol) -> None:
    bot = LLMBot(1, model="local", base_url="http://localhost:11434/v1")
    result = run_match([bot, GreedyBot()], 123, protocol=protocol)
    assert result.outcome == "finished"
    replayed = replay_events(result.events).state
    # The event log preserves unused card membership, not shuffled order.
    assert sorted(replayed.undealt_remainder) == sorted(result.final_state.undealt_remainder)
    assert replayed.model_dump(exclude={"undealt_remainder"}) == result.final_state.model_dump(
        exclude={"undealt_remainder"}
    )
    assert bot.stats()["calls"] == len(endpoint.requests)


def test_engine_rejection_is_returned_to_model(endpoint: Endpoint) -> None:
    endpoint.illegal_card_once = True
    result = run_match([LLMBot(1, model="local", base_url="http://localhost:11434/v1"), GreedyBot()], 123)
    assert result.outcome == "finished"
    assert result.actions_rejected == 1
    retry = json.loads(endpoint.requests[1]["messages"][1]["content"])
    assert retry["rejection"] is not None
    assert retry["previous_action"]["type"] == "select_card"


def test_configured_credentials_stay_out_of_manifest(
    endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("ARENA_TEST_KEY", "test-credential")
    trace_dir = tmp_path / "run"
    result = run_arena(
        [
            PlayerConfig(
                bot="llm",
                options={"model": "local", "base_url": "https://models.example/v1", "api_key_env": "ARENA_TEST_KEY"},
            ),
            PlayerConfig(bot="greedy"),
        ],
        2,
        123,
        config=RunConfig(trace_dir=trace_dir),
    )
    assert result.reproducible is False
    manifest_text = (trace_dir / "manifest.json").read_text()
    assert "test-credential" not in manifest_text
    assert endpoint.authorizations[0] == "Bearer test-credential"
    manifest = json.loads(manifest_text)
    for match in manifest["matches"]:
        assert match["seat_stats"]["player_1"]["calls"] > 0
        assert match["seat_stats"]["player_1"]["response_models"] == ["local-model-revision"]


@pytest.mark.parametrize(
    "options",
    [
        {"base_url": "file:///tmp/model"},
        {"base_url": "https://user:password@example.com/v1"},
        {"base_url": "https://example.com/v1?key=secret"},
        {"repair_attempts": -1},
        {"request_timeout_seconds": 0.0},
        {"model": ""},
        {"api_key": "secret"},
    ],
)
def test_rejects_invalid_model_configuration(options: dict) -> None:
    with pytest.raises(ValidationError):
        LLMOptions.model_validate({"model": "local", "base_url": "http://localhost:11434/v1", **options})


def test_view_carries_public_protocol_limits() -> None:
    protocol = MatchProtocol(negotiation_enabled=True, allow_direct_messages=False, max_message_length=73)
    _, events = create_match("protocol", ["a", "b"], 123, protocol=protocol)
    view = build_view(events, Viewer(ViewRole.PLAYER, "a"))
    assert view.protocol == protocol


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("select_card", {"card": 1}),
        ("choose_row", {"row_index": 2}),
        ("commit", {}),
        ("uncommit", {}),
        ("send_message", {"visibility": "table", "body": "hello"}),
        ("send_message", {"visibility": "direct", "body": "hello", "to_player": "b"}),
    ],
)
def test_parses_each_game_action(view: MatchView, name: str, arguments: dict) -> None:
    from openai.types.chat import ChatCompletion

    from sixnimmt_server.arena.bots.llm import parse_action

    response = ChatCompletion.model_validate({
        "id": "completion",
        "object": "chat.completion",
        "created": 0,
        "model": "local",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
            }
        ],
    })
    action = parse_action(response, view.model_copy(update={"legal_actions": (name,)}))
    assert action.type == name
    assert (
        action.model_dump(
            mode="json", exclude={"type", "action_id", "from_view", "expected_view_version"}, exclude_none=True
        )
        == arguments
    )


@pytest.mark.parametrize(
    "arguments", ['{"card": "1"}', '{"card": true}', '{"card": 1, "from_view": "fake"}', "[]", "{"]
)
def test_rejects_malformed_or_extra_tool_arguments(view: MatchView, arguments: str) -> None:
    from openai.types.chat import ChatCompletion

    from sixnimmt_server.arena.bots.llm import parse_action

    response = ChatCompletion.model_validate({
        "id": "completion",
        "object": "chat.completion",
        "created": 0,
        "model": "local",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call",
                            "type": "function",
                            "function": {
                                "name": "select_card",
                                "arguments": arguments,
                            },
                        }
                    ],
                },
            }
        ],
    })
    with pytest.raises((ValueError, TypeError)):
        parse_action(response, view)


def test_does_not_retry_after_decision_budget_expires(
    endpoint: Endpoint, view: MatchView, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint.invalid_responses = 1
    clock_values = iter([0.0, 0.0, 0.0, 121.0, 121.0])
    monkeypatch.setattr("sixnimmt_server.arena.bots.llm.monotonic", lambda: next(clock_values))
    bot = LLMBot(1, model="local", base_url="http://localhost:11434/v1")
    with pytest.raises(ModelDecisionError, match="budget exhausted"):
        bot.act(view)
    assert len(endpoint.requests) == 1


def test_missing_configured_credentials_fail_before_requests(
    endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MISSING_ARENA_CREDENTIAL", raising=False)
    with pytest.raises(ValueError, match="empty or missing"):
        LLMBot(1, model="local", base_url="https://example.com/v1", api_key_env="MISSING_ARENA_CREDENTIAL")
    assert endpoint.requests == []


def test_seats_use_independent_strategy_and_personality_prompts(endpoint: Endpoint, view: MatchView) -> None:
    cautious = LLMBot(
        1,
        model="local",
        base_url="http://localhost:11434/v1",
        strategy_prompt="Play cautiously and speak diplomatically.",
    )
    bold = LLMBot(
        2,
        model="local",
        base_url="http://localhost:11434/v1",
        strategy_prompt="Take calculated risks and negotiate assertively.",
    )
    cautious.act(view)
    bold.act(view)
    cautious_prompt = endpoint.requests[0]["messages"][0]["content"]
    bold_prompt = endpoint.requests[1]["messages"][0]["content"]
    assert "Play cautiously and speak diplomatically." in cautious_prompt
    assert "Take calculated risks" not in cautious_prompt
    assert "Take calculated risks and negotiate assertively." in bold_prompt
    assert "Play cautiously" not in bold_prompt
    assert cautious.stats()["prompt_sha256"] != bold.stats()["prompt_sha256"]


def test_system_prompt_can_be_replaced_per_seat(endpoint: Endpoint, view: MatchView) -> None:
    bot = LLMBot(
        1,
        model="local",
        base_url="http://localhost:11434/v1",
        system_prompt="Custom game instructions.",
        strategy_prompt="Be cooperative.",
    )
    bot.act(view)
    assert endpoint.requests[0]["messages"][0]["content"] == (
        "Custom game instructions.\nStrategy and personality:\nBe cooperative."
    )
