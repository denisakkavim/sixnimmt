# LLM players

`llm` and `llm_memory` are arena adapters for tool-calling, Chat Completions
compatible endpoints. Endpoint calls belong to bots; the engine has no model
dependency. Each seat can use its own model, endpoint, and strategy prompt.

## Configure a strategy

An arena configuration can include a model strategy in its `catalogue` array.
Replace the model and URL with your provider's values and supply its credential
through the named environment variable:

```json
{
  "catalogue": [
    {
      "bot": "llm",
      "label": "Cautious player",
      "options": {
        "model": "YOUR_TOOL_CAPABLE_MODEL",
        "base_url": "https://your-provider.example/v1",
        "api_key_env": "ARENA_MODEL_KEY",
        "strategy_prompt": "Prefer low-risk plays and keep messages concise."
      }
    }
  ]
}
```

The configured environment variable must exist and be nonempty. If `api_key_env`
is omitted, the adapter uses a placeholder key suitable for endpoints that do
not require authentication. It does not implicitly read `OPENAI_API_KEY`.
The base URL must be an absolute HTTP(S) URL without embedded credentials,
query parameters, or a fragment.

The repository includes [LLM](../examples/arena-llm.json),
[memory comparison](../examples/arena-llm-memory.json), and
[OpenRouter](../examples/arena-openrouter.json) configuration examples. Treat
their model names as example configuration, not availability guarantees.

After editing the configuration for your endpoint:

```bash
uv run sixnimmt play --config examples/arena-llm.json --games 1 --seed 1234 --decision-timeout 130 --output-dir runs/llm-first-run --trace
```

Start with a small game budget and concurrency one to check tool compatibility
and response latency. Random lineups can omit a configuration in a small run;
inspect the report's appearance counts. A model-only catalogue ensures that
every seat uses the adapter. Add `--communication` to offer messages and explicit
commitment. Give different models or prompts using the same bot distinct `key`
values, as in the OpenRouter examples.

For one fixed lineup mixing LLM adapters with Codex or Claude Code, use
[`sixnimmt play --watch --config`](harness-players.md#configure-models-and-a-lineup).
Its file uses the same catalogue entries, and every model is selected through
`options.model`. LLM entries retain the endpoint options below; headless harness
entries use their installed CLI's authentication. The
[mixed-model example](../examples/table-models.json) includes all three adapters.

## Options

Unknown options are rejected. `llm_memory` accepts all of these as well.

| Option | Default | Behavior |
| --- | --- | --- |
| `model` | Required | Model identifier |
| `base_url` | Required | API root |
| `api_key_env` | `null` | Name of credential environment variable |
| `temperature` | `null` | Omitted unless set; range 0–2 |
| `max_tokens` | 2048 | Positive output token limit |
| `token_limit_parameter` | `max_tokens` | May be `max_completion_tokens` |
| `request_timeout_seconds` | 60 | Provider request timeout |
| `decision_budget_seconds` | 120 | Budget across response repair attempts |
| `repair_attempts` | 1 | Additional parsing/format repair attempts; range 0–3 |
| `tool_choice` | `required` | Also accepts `auto` or `null` |
| `disable_parallel_tool_calls` | `false` | When true, sends `parallel_tool_calls=false`; otherwise omits it |
| `strict_tools` | `false` | Request strict function schemas |
| `simplified_tool_schemas` | `false` | Strip schema constraints for endpoint compatibility |
| `provider_options` | `{}` | Additional provider request-body fields |
| `system_prompt` | Built-in rules | Replace shared game instructions |
| `strategy_prompt` | Empty string | Append seat-specific strategy/personality |

Strict and simplified schemas cannot both be enabled. Local validation always
checks argument types, unknown fields, and legality. `provider_options` cannot
override standard generation options, messages, tools, streaming, or credentials.
The adapter passes supported extra fields through without translating provider
dialects; check your endpoint's documentation for their meaning.

For endpoints accepting a `reasoning_effort` request field, add it to
`provider_options`, for example `"provider_options": {"reasoning_effort": "high"}`.
The adapter passes the value through and the endpoint validates supported levels.
Headless Codex and Claude seats use `options.reasoning_effort` alongside
`options.model`; their drivers translate it to client settings rather than an
HTTP request. See [headless options](harness-players.md#headless-options).

The effective decision budget is the shorter of `decision_budget_seconds` and
the remaining arena deadline. Each provider request is bounded by that remaining
budget and `request_timeout_seconds`; repair attempts share the same budget.
Budget exhaustion uses the arena's typed deadline outcome. Setting the arena
`--decision-timeout` above the adapter budget gives cleanup additional time.
A timeout does not
cancel the underlying thread or guarantee that the provider stops processing it.

## Observations and tool calls

Every request includes current rules and match settings, the seat's strategy
prompt, and a text rendering of its filtered observation. Both bot types receive
the same public play history, own hand, permitted messages, and action budget.
The basic `llm` adapter starts fresh at each decision rather than retaining a
growing conversation.

Responses contain exactly one `play` function call. Its `proposal` has the same
shape used by native and headless harnesses over MCP. For a classic decision with
card 42 in hand, the arguments look like this:

```json
{
  "proposal": {
    "decision_id": "COPY_THE_CURRENT_DECISION_ID",
    "submission_id": "A_NEW_UNIQUE_SUBMISSION_ID",
    "actions": [{"type": "select_card", "card": 42}],
    "memory": null,
    "explanation": "This card fits the current row."
  }
}
```

The adapter supplies the decision ID in the observation and constrains it in the
tool schema. Each request rebuilds the nested action schemas: `select_card.card`
is an enum of the current hand, row indices and message recipients are limited
to the current decision, and unavailable action variants are absent. Required
fields, types, and unknown-field rejection remain enforced. Strict schemas also
require optional values such as `explanation` to be present as null when unused.
The explicit `simplified_tool_schemas` option removes compatibility constraints
recursively; it does not disable parsing or game validation.

A proposal contains one to eight operations, counting a non-null notebook update
as one. The arena validates the complete proposal as an
[atomic batch](bots.md#atomic-proposals). Commitment and row choice must end the
game-action sequence. A malformed, missing, or multiple tool call can trigger
bounded repair with the same decision ID and schema. An engine rejection is
returned through the arena's rejection feedback. Provider errors fail the match
without SDK retries or a fallback move.

`explanation` is optional operator commentary, limited to 1,000 characters. It
never becomes a game message or notebook update. Use null when no commentary is
available. API bots must supply a proposal during a decision; `play` with a null
proposal is reserved for MCP session entry and recovery.

Use `strategy_prompt` to compare personalities while retaining shared rules.
Use `system_prompt` to replace those rules. Active mode and match settings are
still injected, and validation remains enforced by code. Shared rules and decision
instructions are composed explicitly with the adapter's delivery instructions;
API and MCP players use the same rule text and typed atomic `play` contract.
API bots still receive fresh context at each decision; native and headless
harnesses retain their own conversation for the match.

## Private memory

Choose `llm_memory` to retain a notebook within a match. Its extra option,
`memory_max_chars`, defaults to 4,000 and accepts 1–16,000.

The proposal's `memory` field supplies a complete replacement notebook. Null
preserves the notebook; an empty string clears it. `llm` requires this field to
be null. `llm_memory` permits memory-only proposals with an empty `actions` array;
the update counts as one operation toward arena limits. A rejected proposal
leaves memory unchanged. There is no separate memory tool.

Each new arena match gets new bot instances and empty notebooks. Memory is a
bounded model-written summary, not guaranteed factual recall. It is absent from
other players' observations, game events, and the manifest, but appears in
privileged model request/response logs.

## Reading results

Model configurations are recorded as nondeterministic: a root seed does not
make a provider reproduce its answers. Recorded game events can still be replayed without calling
the provider. Statistics record usage when supplied, missing usage, requests,
repairs, errors, and latency; the project does not calculate monetary costs.

Trace files may contain private hands, messages, notebook contents, and raw
provider output. Alongside private raw response records, the LLM adapter emits
normalized `model_text`, `reasoning_summary`, `tool_activity`, and
`decision_explanation` records for
operator display. Terminal renderers consume those records without parsing the
provider response. Tool activity describes a requested call; arena acceptance is
reported separately. Authentication headers are not recorded and the configured key
is redacted if echoed, but that does not make arbitrary provider output safe to
publish. See [Traces and replay](traces.md).

Sources: [LLM options and adapter](../src/sixnimmt/arena/bots/llm.py),
[memory adapter](../src/sixnimmt/arena/bots/llm.py), and
[shared rules and action schemas](../src/sixnimmt/arena/bots/agent_contract.py), and
[shared play contract](../src/sixnimmt/arena/bots/agent_protocol.py).

Python callers with validated `LLMOptions` or `LLMMemoryOptions` can pass them as
`validated_options=` to the corresponding bot constructor. Supply either that
model or option keywords. The constructor copies validated settings for the bot;
registry resolution uses this path to avoid serializing and validating them again.
