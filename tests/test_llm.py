"""Model bots use tool calls through configurable endpoints without privileged state."""

import json
from typing import Any

import httpx2 as httpx
import pytest
from openai import OpenAI
from pydantic import ValidationError

from sixnimmt_server.arena.bots import GreedyBot
from sixnimmt_server.arena.bots.base import ActionBatch
from sixnimmt_server.arena.bots.llm import LLMBot, LLMOptions, ModelDecisionError
from sixnimmt_server.arena.bots.llm_memory import LLMMemoryBot, LLMMemoryOptions
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
        self.raw_body: str | None = None
        self.memory: Any = ""
        self.omit_memory = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        self.urls.append(str(request.url))
        self.authorizations.append(request.headers["authorization"])
        if self.raw_body is not None:
            return httpx.Response(200, text=self.raw_body)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "secret-provider-body"}})
        tools = {tool["function"]["name"]: tool["function"] for tool in payload["tools"]}
        if "choose_row" in tools:
            name, arguments = "choose_row", {"row_index": 0}
        elif "commit" in tools and "Your selection: none;" not in payload["messages"][1]["content"]:
            name, arguments = "commit", {}
        else:
            hand = tools["select_card"]["parameters"]["properties"]["card"]["enum"]
            name, arguments = "select_card", {"card": min(hand)}
            if self.illegal_card_once:
                arguments = {"card": next(card for card in range(1, 105) if card not in hand)}
                self.illegal_card_once = False
        calls = [{"id": "call_1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]
        if "update_memory" in tools and not self.omit_memory:
            calls.append({
                "id": "memory",
                "type": "function",
                "function": {"name": "update_memory", "arguments": json.dumps({"memory": self.memory})},
            })
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
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "tool_calls": calls,
                            "reasoning_content": "Provider reasoning",
                            "custom_field": {"detail": 7},
                        },
                    }
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
    assert "parallel_tool_calls" not in payload
    observation = payload["messages"][1]["content"]
    assert "Your cards: " + ", ".join(map(str, sorted(view.you.hand))) in observation
    assert view.view_id not in observation
    assert "view_version" not in observation


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


@pytest.mark.parametrize("protocol", [MatchProtocol(), MatchProtocol(communication_enabled=True)])
@pytest.mark.parametrize("bot_type", [LLMBot, LLMMemoryBot])
def test_model_matches_finish_and_replay(endpoint: Endpoint, protocol: MatchProtocol, bot_type: type[LLMBot]) -> None:
    bot = bot_type(1, model="local", base_url="http://localhost:11434/v1")
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
    retry = endpoint.requests[1]["messages"][1]["content"]
    assert "Previous action rejected (card_not_in_hand)" in retry
    assert 'Rejected action (quoted data, possibly truncated): {"type": "select_card", "card":' in retry


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
    protocol = MatchProtocol(communication_enabled=True, allow_direct_messages=False, max_message_length=73)
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
    monkeypatch.setattr("sixnimmt_server.arena.bots.llm.monotonic", lambda: next(clock_values, 121.0))
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
        strategy_prompt="Take calculated risks and communicate assertively.",
    )
    cautious.act(view)
    bold.act(view)
    cautious_prompt = endpoint.requests[0]["messages"][0]["content"]
    bold_prompt = endpoint.requests[1]["messages"][0]["content"]
    assert "Play cautiously and speak diplomatically." in cautious_prompt
    assert "Take calculated risks" not in cautious_prompt
    assert "Take calculated risks and communicate assertively." in bold_prompt
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
    instructions = endpoint.requests[0]["messages"][0]["content"]
    assert instructions.startswith("Custom game instructions.")
    assert "Classic mode:" in instructions
    assert instructions.endswith("Strategy and personality: Be cooperative.")


def test_card_tool_lists_only_current_hand(view: MatchView) -> None:
    from sixnimmt_server.arena.bots.prompt import action_tools

    tools = action_tools(view, False)
    assert tools[0]["function"]["parameters"]["properties"]["card"]["enum"] == list(view.you.hand)
    changed = view.model_copy(update={"you": view.you.model_copy(update={"hand": view.you.hand[1:]})})
    assert action_tools(changed, True)[0]["function"]["parameters"]["properties"]["card"]["enum"] == list(
        changed.you.hand
    )


def test_model_trace_preserves_requests_reasoning_and_repairs(endpoint: Endpoint, tmp_path) -> None:
    endpoint.invalid_responses = 1
    result = run_arena(
        [
            PlayerConfig(
                bot="llm",
                display_name="Thinker",
                options={
                    "model": "local",
                    "base_url": "http://localhost:11434/v1",
                    "provider_options": {"reasoning_effort": "low"},
                },
            ),
            PlayerConfig(bot="greedy"),
        ],
        1,
        123,
        protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
        config=RunConfig(trace_dir=tmp_path / "run"),
    )
    path = next((tmp_path / "run").glob("*.model.jsonl"))
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert result.finished == 1
    assert [record["kind"] for record in records[:6]] == [
        "request",
        "response",
        "parse_error",
        "request",
        "response",
        "action_parsed",
    ]
    requests = [record for record in records if record["kind"] == "request"]
    assert [record["payload"] for record in requests] == endpoint.requests
    assert all(record["display_name"] == "Thinker" and record["player_id"] == "player_1" for record in records)
    assert records[0]["decision_id"] == records[3]["decision_id"]
    assert records[0]["request_id"] != records[3]["request_id"]
    assert records[3]["attempt"] == 1
    body = json.loads(records[1]["body"])
    assert body["choices"][0]["message"]["reasoning_content"] == "Provider reasoning"
    assert body["choices"][0]["message"]["custom_field"] == {"detail": 7}
    assert body["usage"]["prompt_tokens"] == 100
    assert "decision_summary" not in str(requests[0]["payload"]["tools"])


def test_model_trace_records_provider_errors_and_redacts_credential(
    endpoint: Endpoint, view: MatchView, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint.status = 401
    monkeypatch.setenv("TRACE_KEY", "secret-provider-body")
    records = []
    bot = LLMBot(1, model="local", base_url="http://localhost:11434/v1", api_key_env="TRACE_KEY")
    bot.set_trace(records.append)
    with pytest.raises(ModelDecisionError):
        bot.act(view)
    assert [record["kind"] for record in records] == ["request", "provider_error"]
    assert records[1]["status_code"] == 401
    assert "secret-provider-body" not in json.dumps(records)
    assert "[REDACTED]" in records[1]["body"]


@pytest.mark.parametrize("field", ["messages", "model", "tools", "stream", "api_key"])
def test_provider_options_cannot_override_game_request(field: str) -> None:
    with pytest.raises(ValidationError):
        LLMOptions(model="local", base_url="http://localhost:11434/v1", provider_options={field: "override"})


@pytest.mark.parametrize("body", ["not JSON", '{"unknown": "response"}'])
def test_raw_malformed_responses_are_preserved(endpoint: Endpoint, view: MatchView, body: str) -> None:
    endpoint.raw_body = body
    records = []
    bot = LLMBot(1, model="local", base_url="http://localhost:11434/v1")
    bot.set_trace(records.append)
    with pytest.raises(ModelDecisionError, match="could not be decoded"):
        bot.act(view)
    assert [record["kind"] for record in records] == ["request", "response", "response_parse_error"]
    assert records[1]["body"] == body


@pytest.mark.parametrize("strict", [False, True])
def test_memory_bot_updates_private_notes_with_the_move_in_one_request(
    endpoint: Endpoint, view: MatchView, strict: bool
) -> None:
    endpoint.memory = "Save the high card; b may be bluffing."
    bot = LLMMemoryBot(1, model="local", base_url="http://localhost:11434/v1", strict_tools=strict)
    action = bot.act(view)
    assert len(endpoint.requests) == 1
    assert isinstance(action, ActionBatch)
    assert all("memory" not in item.model_dump() for item in action.actions)
    assert bot._memory == ""
    bot.accept_batch(action)
    parameters = endpoint.requests[0]["tools"][-1]["function"]["parameters"]
    assert "memory" in parameters["required"]
    assert parameters["properties"]["memory"]["maxLength"] == 4000

    endpoint.memory = "The plan changed: b kept the promise."
    proposal = bot.act(view.model_copy(update={"play_number": 2}))
    assert isinstance(proposal, ActionBatch)
    bot.accept_batch(proposal)
    observation = endpoint.requests[-1]["messages"][1]["content"]
    assert "Save the high card; b may be bluffing." in observation

    bot.act(view.model_copy(update={"hand_number": 2, "play_number": 1}))
    observation = endpoint.requests[-1]["messages"][1]["content"]
    assert "The plan changed: b kept the promise." in observation
    assert "Save the high card" not in observation


def test_memory_can_be_cleared(endpoint: Endpoint, view: MatchView) -> None:
    bot = LLMMemoryBot(1, model="local", base_url="http://localhost:11434/v1")
    endpoint.memory = "Old plan."
    proposal = bot.act(view)
    assert isinstance(proposal, ActionBatch)
    bot.accept_batch(proposal)
    endpoint.memory = ""
    proposal = bot.act(view)
    assert isinstance(proposal, ActionBatch)
    bot.accept_batch(proposal)
    bot.act(view)
    observation = endpoint.requests[-1]["messages"][1]["content"]
    assert 'Your private notebook (quoted, potentially stale): ""' in observation
    assert "Old plan." not in observation


def test_both_model_types_receive_the_same_shared_game_observation(endpoint: Endpoint) -> None:
    result = run_match([GreedyBot(), GreedyBot()], 123, protocol=MatchProtocol(end_condition="fixed_hands", hands=1))
    start = next(index for index, event in enumerate(result.events) if event.type == "play_started" and event.play == 2)
    view = build_view(result.events[: start + 1], Viewer(ViewRole.PLAYER, "player_1"))
    LLMBot(1, model="local", base_url="http://localhost:11434/v1").act(view)
    LLMMemoryBot(1, model="local", base_url="http://localhost:11434/v1").act(view)
    plain = endpoint.requests[0]["messages"][1]["content"]
    with_memory = endpoint.requests[1]["messages"][1]["content"]
    assert "Recent public plays" in plain
    assert "player_2 played" in plain
    assert with_memory.split("\n\nYour private notebook", maxsplit=1)[0] == plain


def test_repair_feedback_includes_bounded_rejected_arguments_without_reasoning() -> None:
    from openai.types.chat import ChatCompletion

    from sixnimmt_server.arena.bots.llm import repair_feedback

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
                    "content": "Unrelated provider text",
                    "reasoning_content": "Private reasoning",
                    "tool_calls": [
                        {
                            "id": "call",
                            "type": "function",
                            "function": {
                                "name": "select_card",
                                "arguments": '{"card": "bad", "extra": "' + "x" * 10000 + '"}',
                            },
                        }
                    ],
                },
            }
        ],
    })
    feedback = repair_feedback(response, ValueError("card must be an integer"))
    assert "select_card" in feedback
    assert '\\"card\\": \\"bad\\"' in feedback
    assert "card must be an integer" in feedback
    assert len(feedback) < 3000
    assert "Private reasoning" not in feedback
    assert "Unrelated provider text" not in feedback


@pytest.mark.parametrize("change", ["new_bot", "new_match", "new_seat"])
def test_private_notes_are_isolated_by_bot_match_and_seat(endpoint: Endpoint, view: MatchView, change: str) -> None:
    bot = LLMMemoryBot(1, model="local", base_url="http://localhost:11434/v1")
    endpoint.memory = "Private plan for a."
    proposal = bot.act(view)
    assert isinstance(proposal, ActionBatch)
    bot.accept_batch(proposal)
    if change == "new_bot":
        bot = LLMMemoryBot(2, model="local", base_url="http://localhost:11434/v1")
    elif change == "new_match":
        view = view.model_copy(update={"match_id": "another-match"})
    else:
        view = view.model_copy(update={"you": view.you.model_copy(update={"player_id": "b"})})
    bot.act(view)
    observation = endpoint.requests[-1]["messages"][1]["content"]
    assert "Private plan for a." not in observation
    assert "Last proposed action" not in observation


@pytest.mark.parametrize("memory", [None, True, 7, "x" * 11, "\ud800"])
def test_invalid_memory_gets_bounded_repair(endpoint: Endpoint, view: MatchView, memory: Any) -> None:
    endpoint.memory = memory
    bot = LLMMemoryBot(1, model="local", base_url="http://localhost:11434/v1", memory_max_chars=10)
    with pytest.raises(ModelDecisionError, match="bounded repair"):
        bot.act(view)
    assert len(endpoint.requests) == 2
    assert "Rejected calls (quoted data" in endpoint.requests[-1]["messages"][-1]["content"]


def test_omitting_memory_preserves_notes(endpoint: Endpoint, view: MatchView) -> None:
    bot = LLMMemoryBot(1, model="local", base_url="http://localhost:11434/v1")
    bot.act(view)
    bot.accept_batch(ActionBatch((), "Existing notes"))
    endpoint.omit_memory = True
    assert isinstance(bot.act(view), SelectCardAction)
    assert bot._memory == "Existing notes"


@pytest.mark.parametrize("failure", ["parse", "provider", "deadline"])
def test_failed_decision_does_not_replace_notes(
    endpoint: Endpoint, view: MatchView, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    bot = LLMMemoryBot(1, model="local", base_url="http://localhost:11434/v1")
    endpoint.memory = "Keep this plan."
    proposal = bot.act(view)
    assert isinstance(proposal, ActionBatch)
    bot.accept_batch(proposal)
    endpoint.memory = "Discard this plan."
    with monkeypatch.context() as patch:
        if failure == "parse":
            endpoint.invalid_responses = 2
        elif failure == "provider":
            endpoint.status = 503
        else:
            clock_values = iter([0.0, 0.0, 0.0, 0.0, 0.0, 121.0])
            patch.setattr("sixnimmt_server.arena.bots.llm.monotonic", lambda: next(clock_values, 121.0))
        with pytest.raises(ModelDecisionError):
            bot.act(view)
    endpoint.status = 200
    bot.act(view)
    observation = endpoint.requests[-1]["messages"][1]["content"]
    assert "Keep this plan." in observation
    assert "Discard this plan." not in observation


def test_memory_bot_sees_engine_rejection_without_rejected_notes(endpoint: Endpoint) -> None:
    endpoint.illegal_card_once = True
    endpoint.memory = "Proposed the smallest card."
    result = run_match(
        [LLMMemoryBot(1, model="local", base_url="http://localhost:11434/v1"), GreedyBot()],
        123,
        protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
    )
    assert result.outcome == "finished"
    assert result.actions_rejected == 2
    observation = endpoint.requests[1]["messages"][1]["content"]
    assert "Previous action rejected (card_not_in_hand)" in observation
    assert "Proposed the smallest card." not in observation
    assert "Nothing was applied; memory is unchanged" in observation


def test_registered_memory_bots_start_each_match_fresh_without_publishing_notes(endpoint: Endpoint, tmp_path) -> None:
    endpoint.memory = "A private long-term plan."
    result = run_arena(
        [
            PlayerConfig(
                bot="llm_memory",
                options={
                    "model": "local",
                    "base_url": "http://localhost:11434/v1",
                    "memory_max_chars": 100,
                },
            ),
            PlayerConfig(bot="greedy"),
        ],
        2,
        123,
        protocol=MatchProtocol(end_condition="fixed_hands", hands=2),
        config=RunConfig(trace_dir=tmp_path / "run"),
    )
    assert result.finished == 2
    assert result.reproducible is False
    for path in (tmp_path / "run").glob("*.model.jsonl"):
        records = [json.loads(line) for line in path.read_text().splitlines()]
        requests = [entry for entry in records if entry["kind"] == "request"]
        assert (
            'Your private notebook (quoted, potentially stale): ""' in requests[0]["payload"]["messages"][1]["content"]
        )
        second_hand = next(entry for entry in requests if entry["hand"] == 2)
        assert "A private long-term plan." in second_hand["payload"]["messages"][1]["content"]
        event_path = path.with_name(path.name.replace(".model.jsonl", ".jsonl"))
        assert "A private long-term plan." not in event_path.read_text()
    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text())
    assert manifest["seats"][0]["options"]["memory_max_chars"] == 100
    assert "A private long-term plan." not in json.dumps(manifest)


@pytest.mark.parametrize("limit", [0, 16001, True, "100"])
def test_memory_options_reject_invalid_limits(limit: Any) -> None:
    with pytest.raises(ValidationError):
        LLMMemoryOptions.model_validate({
            "model": "local",
            "base_url": "http://localhost:11434/v1",
            "memory_max_chars": limit,
        })


class CommunicatingEndpoint(Endpoint):
    """Script a request/reply and revised selection, then finish the hand."""

    def __init__(self) -> None:
        super().__init__()
        self.offers = {"alice": 0, "bob": 0}
        self.alice_initial_card: int | None = None
        self.alice_revised_card: int | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        response = super().handle(request)
        payload = self.requests[-1]
        model = payload["model"]
        observation = payload["messages"][1]["content"]
        tools = {tool["function"]["name"]: tool["function"] for tool in payload["tools"]}
        offer = self.offers[model]
        self.offers[model] += 1
        body = response.json()
        function = body["choices"][0]["message"]["tool_calls"][0]["function"]

        if model == "alice" and offer == 0:
            hand = tools["select_card"]["parameters"]["properties"]["card"]["enum"]
            self.alice_initial_card = min(hand)
            self.alice_revised_card = max(hand)
        elif model == "alice" and offer == 1:
            assert f"Your selection: {self.alice_initial_card}; committed: no." in observation
            function.update(
                name="send_message",
                arguments=json.dumps({
                    "visibility": "table",
                    "body": "Should I switch to my highest card?",
                }),
            )
        elif model == "bob" and offer == 1:
            assert 'player_1 → table: "Should I switch to my highest card?"' in observation
            function.update(
                name="send_message",
                arguments=json.dumps({
                    "visibility": "direct",
                    "to_player": "player_1",
                    "body": "Yes, switch to your highest card.",
                }),
            )
        elif model == "alice" and offer == 2:
            assert 'player_2 → player_1: "Yes, switch to your highest card."' in observation
            assert f"Your selection: {self.alice_initial_card}; committed: no." in observation
            function.update(name="select_card", arguments=json.dumps({"card": self.alice_revised_card}))
        elif model == "bob" and offer == 2:
            assert "player_1: selected, not committed." in observation
            assert function["name"] == "commit"
        elif model == "alice" and offer == 3:
            assert f"Your selection: {self.alice_revised_card}; committed: no." in observation
            assert "player_2: committed." in observation
            assert function["name"] == "commit"
        return httpx.Response(200, json=body)


def test_model_seats_exchange_messages_revise_selection_and_finish_communication_match(
    endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    communication = CommunicatingEndpoint()
    # Reuse the real SDK and HTTP transport fixture; only endpoint responses are scripted.
    monkeypatch.setattr(endpoint, "handle", communication.handle)
    bots = [
        LLMBot(seed, model=model, base_url="http://localhost:11434/v1") for seed, model in enumerate(("alice", "bob"))
    ]
    result = run_match(
        bots,
        123,
        protocol=MatchProtocol(communication_enabled=True, end_condition="fixed_hands", hands=1),
        config=RunConfig(match_action_limit=100),
    )

    assert result.outcome == "finished", result.reason
    assert result.actions_rejected == 0
    assert all(bot.stats()["repairs"] == 0 for bot in bots)
    first_play = [event for event in result.events if event.play == 1]
    messages = [event for event in first_play if event.type == "message_sent"]
    assert [
        (event.data["from"], event.data["body"])
        for event in messages
        if event.audience in ("public", "player:player_1")
    ] == [
        ("player_1", "Should I switch to my highest card?"),
        ("player_2", "Yes, switch to your highest card."),
    ]
    alice_selections = [
        event.data["card"]
        for event in first_play
        if event.type == "selection_made" and event.data["player_id"] == "player_1"
    ]
    assert alice_selections == [communication.alice_initial_card, communication.alice_revised_card]
    assert alice_selections[0] != alice_selections[1]
    reveal = next(event for event in first_play if event.type == "cards_revealed")
    assert reveal.data["selections"]["player_1"] == communication.alice_revised_card
    commits = [event for event in first_play if event.type == "player_committed"]
    assert {event.data["player_id"] for event in commits} == {"player_1", "player_2"}
    assert all(event.seq < reveal.seq for event in commits)
    replayed = replay_events(result.events).state
    assert sorted(replayed.undealt_remainder) == sorted(result.final_state.undealt_remainder)
    assert replayed.model_dump(exclude={"undealt_remainder"}) == result.final_state.model_dump(
        exclude={"undealt_remainder"}
    )


def test_rejects_strict_and_simplified_schemas_together() -> None:
    with pytest.raises(ValidationError, match="cannot both be enabled"):
        LLMOptions(model="test", base_url="http://localhost/v1", strict_tools=True, simplified_tool_schemas=True)


def atomic_response(calls: list[tuple[str, dict[str, Any]]]) -> str:
    return json.dumps({
        "id": "atomic",
        "object": "chat.completion",
        "created": 0,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": f"call_{index}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                        for index, (name, arguments) in enumerate(calls)
                    ],
                },
            }
        ],
    })


@pytest.mark.parametrize("position", [0, 1, 2])
def test_memory_tool_position_does_not_change_game_order(endpoint: Endpoint, view: MatchView, position: int) -> None:
    view = view.model_copy(update={"protocol": MatchProtocol(communication_enabled=True)})
    calls = [("select_card", {"card": min(view.you.hand)}), ("commit", {})]
    calls.insert(position, ("update_memory", {"memory": "new notes"}))
    endpoint.raw_body = atomic_response(calls)
    bot = LLMMemoryBot(1, model="test", base_url="http://localhost/v1")
    proposal = bot.act(view)
    assert isinstance(proposal, ActionBatch)
    assert [action.type for action in proposal.actions] == ["select_card", "commit"]
    assert proposal.memory == "new notes"
    assert bot._memory == ""
    bot.accept_batch(proposal)
    assert bot._memory == "new notes"


@pytest.mark.parametrize(
    "calls",
    [
        [],
        [("update_memory", {"memory": "one"}), ("update_memory", {"memory": "two"})],
        [("update_memory", {"memory": ""})] * 9,
        [("update_memory", {"memory": "okay"}), ("select_card", {"card": "bad"})],
    ],
)
def test_invalid_transaction_is_rejected_without_memory_changes(endpoint: Endpoint, view: MatchView, calls) -> None:
    endpoint.raw_body = atomic_response(calls)
    bot = LLMMemoryBot(1, model="test", base_url="http://localhost/v1", repair_attempts=0)
    with pytest.raises(ModelDecisionError):
        bot.act(view)
    assert bot._memory == ""


def test_simplified_memory_tool_retains_local_length_validation(endpoint: Endpoint, view: MatchView) -> None:
    endpoint.raw_body = atomic_response([("update_memory", {"memory": "too long"})])
    bot = LLMMemoryBot(
        1,
        model="test",
        base_url="http://localhost/v1",
        memory_max_chars=2,
        simplified_tool_schemas=True,
        repair_attempts=0,
    )
    with pytest.raises(ModelDecisionError):
        bot.act(view)
    tools = {tool["function"]["name"]: tool["function"] for tool in endpoint.requests[0]["tools"]}
    assert "memory" not in tools["select_card"]["parameters"]["properties"]
    assert tools["update_memory"]["parameters"]["properties"]["memory"]["type"] == "string"
    assert "maxLength" not in tools["update_memory"]["parameters"]["properties"]["memory"]
