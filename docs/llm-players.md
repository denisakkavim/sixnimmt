# LLM players

`llm` and `llm_memory` are arena adapters for tool-calling, Chat Completions
compatible endpoints. Endpoint calls belong to bots; the engine has no model
dependency. Each seat can use its own model, endpoint, and strategy prompt.

## Configure a seat

This is one entry in a player array; replace the model and URL with your provider's
values and supply its credential through the named environment variable:

```json
{
  "bot": "llm",
  "display_name": "Cautious player",
  "options": {
    "model": "YOUR_TOOL_CAPABLE_MODEL",
    "base_url": "https://your-provider.example/v1",
    "api_key_env": "ARENA_MODEL_KEY",
    "strategy_prompt": "Prefer low-risk plays and keep messages concise."
  }
}
```

The configured environment variable must exist and be nonempty. If `api_key_env`
is omitted, the adapter uses a placeholder key suitable for endpoints that do
not require authentication. It does not implicitly read `OPENAI_API_KEY`.
The base URL must be an absolute HTTP(S) URL without embedded credentials,
query parameters, or a fragment.

The repository includes [LLM](../examples/arena-llm-players.json),
[memory comparison](../examples/arena-llm-memory-players.json), and
[OpenRouter](../examples/arena-openrouter-players.json) lineup examples. Treat
their model names as example configuration, not availability guarantees.

After editing the lineup for your endpoint:

```bash
uv run sixnimmt arena --players-file examples/arena-llm-players.json --games 1 --seed 1234 --decision-timeout 130 --trace-dir traces/llm-first-run
```

Start with one match and concurrency one to check tool compatibility and response
latency. Add `--communication` to offer messages and explicit commitment.

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

Set the arena `--decision-timeout` above the adapter's `decision_budget_seconds`.
The provider timeout and adapter budget are useful bounds, but the arena deadline
is the outer protection against a call that does not return. A timeout does not
cancel the underlying thread or guarantee that the provider stops processing it.

## Observations and tool calls

Every request includes current rules and match settings, the seat's strategy
prompt, and a text rendering of its filtered observation. Both bot types receive
the same public play history, own hand, permitted messages, and action budget.
The basic `llm` adapter starts fresh at each decision rather than retaining a
growing conversation.

Responses contain one to eight function calls. The adapter parses a proposal;
the arena then validates it as an [atomic batch](bots.md#atomic-proposals).
Commitment and row choice must end the game-action sequence. A malformed or
missing tool call can trigger bounded repair; an engine rejection is returned
through the arena's rejection feedback. Provider errors fail the match without
SDK retries or a fallback move.

Use `strategy_prompt` to compare personalities while retaining shared rules.
Use `system_prompt` to replace those rules. Active mode and match settings are
still injected, and validation remains enforced by code.

## Private memory

Choose `llm_memory` to retain a notebook within a match. Its extra option,
`memory_max_chars`, defaults to 4,000 and accepts 1–16,000.

The `update_memory` tool supplies a complete replacement notebook. It can occur
at most once per response, anywhere among the game calls. Omitting it preserves
the notebook; an empty string clears it. Memory-only batches are allowed and
count toward arena limits. A rejected batch leaves memory unchanged.

Each new arena match gets new bot instances and empty notebooks. Memory is a
bounded model-written summary, not guaranteed factual recall. It is absent from
other players' observations, game events, and the manifest, but appears in
privileged model request/response logs.

## Reading results

Model runs are marked non-reproducible: a root seed does not make a provider
repeat its answers. Recorded game events can still be replayed without calling
the provider. Statistics record usage when supplied, missing usage, requests,
repairs, errors, and latency; the project does not calculate monetary costs.

Trace files may contain private hands, messages, notebook contents, and raw
provider output. Authentication headers are not recorded and the configured key
is redacted if echoed, but that does not make arbitrary provider output safe to
publish. See [Traces and replay](traces.md).

Sources: [LLM options and adapter](../src/sixnimmt/arena/bots/llm.py),
[memory adapter](../src/sixnimmt/arena/bots/llm_memory.py), and
[prompt/tool construction](../src/sixnimmt/arena/bots/prompt.py).
