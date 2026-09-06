# sixnimmt-server

A deterministic 6 nimmt! rules engine and in-process arena for trusted bots.

## Requirements

- Python 3.13 or newer
- [uv](https://docs.astral.sh/uv/)

## Setup

Install the project and its development tools into a managed virtual environment:

```bash
uv sync --all-groups
```

Configure each seat in a JSON file, then run the arena:

```bash
uv run sixnimmt arena --players-file examples/arena-players.json --games 10000 --seed 1234
```

The players file is an array of structured player configurations, shown below. The `--match-action-limit` (also accepted as `--max-actions-per-match`) defaults to 10,000 attempts, including rejected actions.

The CLI defaults to seed `66`, a nod to the game's penalty-point threshold; use `--seed` to override it.

The command reports the root seed, aggregate hand and action counts, and per-seat wins, ties, total scores, and average scores. Wins count sole winners; ties count matches in which that seat shared the lowest score. Lower scores are better. Finished, abandoned, forfeited, and failed matches are counted separately. Only finished matches contribute scores, wins, and ties. Registered deterministic bots reproduce their results with the same Python runtime and seed, independently of match concurrency.

## Determinism

Each game and bot gets an independent deterministic seed. The arena hashes

```text
sixnimmt-arena:{seed}:{domain}:{game_index}:{seat_index}
```

with SHA-256 and interprets the first eight digest bytes as an unsigned big-endian integer. Domain separation (`match` versus `bot`) and zero-based game and seat indices keep one random stream from depending on how many values another stream consumes. The match domain uses the literal `None` for the seat index. Each match then derives its hand shuffle seeds deterministically from its match seed.

The shuffle implementation intentionally targets Python's `random.Random` behavior rather than defining a cross-language protocol. Known-answer tests protect the expected decks from accidental runtime changes.

## In-process use

```python
from sixnimmt_server.arena.bots import RandomBot
from sixnimmt_server.arena.runner import run_match

result = run_match([RandomBot(11), RandomBot(22)], seed=1234)
print(result.winners)
print([(player.player_id, player.total_score) for player in result.final_state.players])
```

Custom trusted bots implement `act(view: MatchView, rejection: Rejection | None = None) -> Action`.
The view is folded from that seat's audience-filtered events, exactly as it is for
an HTTP player. Bots may return any action; the engine validates it. A refusal
retries the same seat with its error code, message, legal actions, and refreshed
view. Eight consecutive rejections in one offer forfeit the match by default.
An exception or malformed return fails that match and lets the run continue.

Configure a multi-match arena with one `PlayerConfig` per seat:

```python
from sixnimmt_server.arena.players import PlayerConfig
from sixnimmt_server.arena.runner import run_arena

result = run_arena(
    players=[
        PlayerConfig(bot="random", display_name="Alice", agent_metadata={"group": "baseline"}),
        PlayerConfig(bot="greedy", display_name="Bob"),
    ],
    games=100,
    seed=1234,
)
```

The equivalent CLI file is:

```json
[
  {"bot": "random", "display_name": "Alice", "options": {}, "agent_metadata": {"group": "baseline"}},
  {"bot": "greedy", "display_name": "Bob", "options": {}}
]
```

`bot` selects a registered type. `options` configures that instance;
`display_name` and `agent_metadata` configure the seat's identity. Seat IDs remain
`player_1`, `player_2`, etc. Omitted display names become `Player 1`, `Player 2`,
etc. Name-only player lists and the old `--players` flag are no longer accepted.
The bundled random and greedy strategies currently have no strategy options and
reject nonempty option dictionaries.

To register a configurable bot, subclass `BotOptions` in `arena.bots` with its
Pydantic fields, defaults, and constraints, then supply that class as
`BotSpec.options_model`. The arena validates all seats before constructing any
bot and calls `BotSpec.build(seed, **validated_options)` for every match. Existing
seed-only factories work with the default empty `BotOptions` schema. Unknown
option keys are rejected. Each factory receives its own copy of nested options.
For example, a model-backed bot can declare `model`, `temperature`, and `prompt`
fields; two seats can then select the same registered type with different values.

The manifest records resolved options, including defaults, and display names.
Seat metadata combines the registry metadata with per-player metadata (the
per-player values take precedence), plus an arena-supplied `bot_options` entry.
Options and metadata are recorded in traces; credentials should come from the
bot's environment or client setup. Bot settings stay out of other players' views.

Declare `BotSpec.deterministic` honestly for the configurations it accepts:
a seeded deal does not reproduce an external model's answers. Optional `stats()`
returns opaque JSON data for the manifest; missing or failed statistics never
change a match outcome.

`run_match` and `run_arena` accept `GameRules`, `MatchProtocol`, and `RunConfig`.
An optional `observer(state, events)` receives setup and every appended batch.
It is privileged instrumentation with authoritative state and must be thread-safe
when matches run concurrently.

## Traced experiments

```bash
uv run sixnimmt arena --players-file examples/arena-players.json --games 1000 --seed 1234 \
  --communication --scheduler round_robin --concurrency 8 --trace-dir traces/run-1234
uv run sixnimmt summarise traces/run-1234/<match-log>.jsonl
uv run sixnimmt replay traces/run-1234/<match-log>.jsonl
```

`--trace-dir` must name a new directory. It contains event logs, action records
for accepted, rejected, failed, and timed-out decisions, and `manifest.json` with
the configuration, seat metadata, derived seeds, statistics, and outcomes.
The summary command automatically reads the adjacent manifest; `--manifest`
selects another. Without a manifest it explicitly reports that the detailed
reason for an abandonment is unavailable. Decision latency comes from monotonic
measurements in arena records and is unavailable for HTTP records.

Without tracing, the arena retains only aggregate results across games. Use
that mode for smoke tests and volume checks; keep traces for experiments.

With communication enabled, scheduling defaults to `round_robin`, offering uncommitted seats in rotation.
Classic defaults to `sequential`; pairing that scheduler with communication is refused because
one noncommitting seat could starve everyone else. Required row choices always
go directly to the awaited player. The stock schedulers skip committed seats,
so those seats cannot initiate an uncommit through these scheduling policies.

`--play-action-limit` defaults to 200 with communication enabled and is unset in classic.
It counts attempts across all seats and resets on the next play. It is separate
from the engine's per-player `MatchProtocol.max_actions_per_play` game rule.
`--decision-rejection-limit` and `--match-action-limit` bound retries and matches.
A completed match always wins over a limit reached by its final action.

## Bot calls and deadlines

Bots are trusted in-process code. A model-backed run should set
`--decision-timeout SECONDS`, and the bot should also impose client-side timeouts.
With a deadline, the arena calls the bot on a daemon thread so a hung call cannot
block the match worker from recording its failure. Python cannot cancel that
call; its late result is discarded. `--max-abandoned-decisions` bounds calls
still running after timeout (default four times concurrency); exceeding it
stops submission and reports a run error. Statistics are skipped for a timed-out
seat because its call may still hold locks or mutate its state.

`--stop-on-failure` stops new submissions after a failed match and drains matches
already started. Without a decision timeout, that drain can wait indefinitely.
The output reports requested, started, and completed counts. Without a deadline,
bots run inline on the match worker with no extra decision thread.

## Tests

Run the default test suite, which excludes longer arena simulations:

```bash
uv run pytest
```

Run only the CLI tests:

```bash
uv run pytest tests/test_cli.py
```

Run the high-volume suite explicitly: 1,000 games each at 2, 3, 5, and 10 players, checking intermediate placements, card conservation, score consistency, hand completion, and bot information boundaries:

```bash
uv run pytest -m arena_slow
```

Run formatting, linting, and type checks:

```bash
uv run ruff format --check .
uv run ruff check .
uv run ty check
```

## Communication and commitment

Set `protocol.communication_enabled` to `true` when creating an HTTP match to enable
explicit commitment, uncommit, table messages, and direct messages. Send a message
through `/matches/{id}/message` (or `/actions` with `type: "send_message"`):

```json
{"visibility": "direct", "to_player": "bob", "body": "Which row would you take?", "action_id": "message-1"}
```

Messaging is optional; players can select a card and commit without sending any
messages. The CLI flag is `--communication`, and the disabled-mode error code is
`COMMUNICATION_DISABLED` (`communication_disabled` in engine rejections).
The former mode name is not accepted as an alias. Update existing configurations
and the protocol keys in saved logs before building views from them; unknown
protocol settings are rejected rather than silently selecting classic mode.

For a table message, use `visibility: "table"` and omit `to_player`. Messages are
accepted only during selection. Self-directed messages are refused. Empty bodies
are allowed; `max_message_length` counts Unicode characters (default 2000), and
text must be representable as UTF-8.

The state view includes `messages`, `private_messages_observed`, and
`messages_omitted`. Together the first two lists retain the latest 100 eligible
messages from the current play and reset when the next play starts. Direct-message
content appears once for each participant or privileged observer. Other viewers
see only the parties when `information_policy.private_message_existence` is
`"visible"`; with `"hidden"`, their views and cursors do not change.
The additional `message_history` list retains the latest 100 eligible messages
across plays and hands, in visible event order. Each entry includes `hand_number`,
`play_number`, and a `message` with the same content/occurrence visibility rules.
Use `/events` for a complete history beyond these bounded windows.

All player and spectator state views also include `play_history`: the latest 20
publicly revealed plays, oldest first, retained across hand boundaries. Each play
has `hand_number`, `play_number`, and `cards` in ascending placement order. Each
card records `player_id`, `card`, `row_index` (null until placed), and `captured`
cards. Selections enter this history only when publicly revealed. The existing
`revealed_this_hand` field remains available for clients that only need card values.
HTTP clients, scripted bots, and both LLM bot types receive these shared histories;
no bot gets privileged access through its memory implementation.

Selections, commits, uncommits, and messages each consume one action when
`max_actions_per_play` is set. Required row choices remain available at zero
budget. Counts and remaining budgets are private to the actor, including with a
finite budget, because public budgets would expose hidden messages. New logs
record every count; older logs retain their historical selection-only accounting.

The message cap bounds response size, not server resource use: live event history
and subscriber queues remain unbounded, and subscribers can accumulate queued events. Views on both surfaces advance
incrementally as events arrive. Harnesses must still bound experiments and
abandon stalled matches. Communication is available through both HTTP and the arena.

## LLM arena players

The `llm` strategy calls an **OpenAI-compatible Chat Completions endpoint** using
function tools. Each seat specifies its own `base_url` and `model`, so one lineup
can compare local models and hosted providers. Endpoints must support tool calling;
this does not imply support for every provider's native API.

For Ollama, start the service and edit the example model name to match an installed
tool-capable model. The example names `qwen3.5:0.8b-mlx`, which was available in
the local environment used for the initial smoke test:

```bash
ollama list
uv run sixnimmt arena --players-file examples/arena-llm-players.json \
  --games 1 --seed 1234 --concurrency 1 --decision-timeout 130 \
  --trace-dir traces/llm-first-run
```

The example uses `http://localhost:11434/v1`. Ollama's compatibility endpoint
accepts the SDK's placeholder API key; no OpenAI account is required. See
[Ollama's compatibility documentation](https://docs.ollama.com/api/openai-compatibility).
The example is a starting configuration, not a model-quality benchmark.

For a hosted endpoint, set `base_url` to its API root (including `/v1` when
required), `model` to its model identifier, and `api_key_env` to the **name** of an
environment variable holding that endpoint's key. Keys are never read implicitly
from `OPENAI_API_KEY`; selecting a different endpoint cannot silently reuse it.
When `api_key_env` is set, an absent or empty variable fails before matches start.
Never put credentials in `options`, `agent_metadata`, or the URL.

Each decision sends concise rules with the active mode and actual match settings
in the system message, followed by the seat personality. A separate user message
renders the filtered view as text: sorted hand, rows and penalties, scores, and
visible-card history. Both `llm` and `llm_memory` receive the same shared game
observation, including recent public moves with player attribution, hand/play
numbers, placement order, pending placements, and captured cards. Communication
adds selections, commitments, recent visible messages and remaining action
budget; row choices identify the triggering card. Rejections include the proposed
action. Transport metadata is excluded from the observation. The `llm` bot starts
fresh each decision, while `llm_memory` also retains a private notebook.
Only currently available action types are offered as
tools. Exactly one function call is accepted; the arena then regains control.
Classic selection and row choice, and messaging and explicit commitment, use
the same path. Add `--communication` to enable communication. Views include the public
protocol so bots can see message permissions, limits, and the end condition.

Model options (unknown keys are rejected):

| Option | Default | Meaning |
|---|---|---|
| `model`, `base_url` | Required | Model identifier and endpoint API root |
| `api_key_env` | `null` | Credential environment variable name; otherwise a placeholder key |
| `temperature` | `null` | Omitted unless specified |
| `max_tokens` | `2048` | Output token cap, including reasoning where the endpoint counts it |
| `token_limit_parameter` | `"max_tokens"` | Set to `"max_completion_tokens"` for endpoints requiring that field |
| `request_timeout_seconds` | `60.0` | SDK request timeout |
| `decision_budget_seconds` | `120.0` | Budget across response repair attempts |
| `repair_attempts` | `1` | Additional requests for malformed/missing/multiple tool calls (0–3) |
| `tool_choice` | `"required"` | Use `"auto"` or `null` to omit it for endpoints with limited support |
| `disable_parallel_tool_calls` | `true` | Sends `parallel_tool_calls=false`; set false to omit the field |
| `strict_tools` | `false` | Opt into server-side strict function schemas where supported |
| `simplified_tool_schemas` | `false` | Omit enum, range, length, and additional-property constraints for endpoint compatibility; local validation remains active. Cannot combine with `strict_tools`. |
| `provider_options` | `{}` | Additional provider request-body fields, such as reasoning controls |
| `system_prompt` | Built-in game instructions | Replace shared rules; mode and match settings are injected |
| `strategy_prompt` | `""` | Seat-specific strategy and personality appended to system instructions |

Tools enumerate cards in the current hand and displayed row indices. Message
tools expose the actual character limit, permitted visibility, and other players'
IDs as recipients (with null for table messages). The recipient description
explains its relationship to visibility; action validation checks that relationship.
Local parsing always rejects extra fields and incorrect argument types, regardless
of `strict_tools`. Well-formed illegal actions reach the engine and its existing
rejection/retry budget. Malformed responses receive bounded repair feedback with
a quoted preview of the rejected tool names and arguments, excluding provider reasoning.
Provider errors fail the match without SDK retries or a fallback move, making
provider failures visible in comparisons. Error bodies are excluded from recorded
failure reasons.

Set the arena's `--decision-timeout` slightly above `decision_budget_seconds`.
The budget caps time allotted to subsequent requests and discards late responses;
SDK network timeouts are not a guaranteed wall-clock cancellation. The arena's
outer deadline remains the backstop. Start with concurrency 1 for a local model.

Tracing records resolved model options, prompt version/hash, returned model
identifiers, calls, repairs, errors, token usage, and request latency per seat.
Missing usage is counted explicitly. Monetary cost is not currently calculated. Raw model interactions are recorded
separately when tracing is enabled. Model runs are marked non-reproducible; game event logs
still replay without calling a model. The root seed controls deals and scripted
bots, and is not sent as a claim of model determinism.


Bot implementations live together in `src/sixnimmt_server/arena/bots/`:
`random.py`, `greedy.py`, `llm.py`, and `llm_memory.py`. Shared contracts are in `base.py`, the
registry is in `__init__.py`, and default LLM instructions and tool schemas are
in `prompt.py`. Existing imports from `sixnimmt_server.arena.bots` still work.

Give each LLM seat its own `options.strategy_prompt` to compare strategies or
personalities, even when both seats use the same model. For example:

```json
{
  "bot": "llm",
  "display_name": "Assertive communicator",
  "options": {
    "model": "qwen3.5:0.8b-mlx",
    "base_url": "http://localhost:11434/v1",
    "strategy_prompt": "Take calculated risks. Communicate assertively and propose mutually beneficial deals."
  }
}
```

Use `options.system_prompt` when you want to replace the default game instructions
for the shared rules. Active mode and match settings are always injected from
the view, then `strategy_prompt` is appended.
Tool schemas and one-action validation remain enforced by code. Both resolved
prompts are recorded in the seat options, and statistics include the hash of the
combined instructions so prompt variants can be distinguished in experiments.


### LLM bots with memory

Choose `"bot": "llm_memory"` to retain a private notebook across decisions,
plays, and hands within a match. It accepts all `llm` options plus
`memory_max_chars` (default 4000, range 1–16000).
Each available tool requires an additional `memory` string containing the
complete replacement notebook. The model can preserve plans, promises, and
observations about opponents, or clear the notebook with an empty string.
Updating memory uses the same response as the move, without a second model call.
The notebook shares the response's output token budget with the action and reasoning.

Only successfully parsed decisions returned within the bot's decision budget
replace the notebook. An engine rejection can still follow; the next observation
includes the rejected action and tells the model to treat its notes as tentative.
Notes are bounded summaries written by the model, so they can omit or misinterpret
facts. They are not a growing conversation or a guarantee of better gameplay.
Arena runs construct a fresh bot for each match, and notebooks are isolated per seat.

The memory field is stripped before constructing the game action. Other players,
game event logs, and run manifests do not receive notebook contents. Privileged
model traces contain the notebook in requests and responses, and statistics
record its format version and configured character limit.

[`examples/arena-llm-memory-players.json`](examples/arena-llm-memory-players.json)
compares `llm` and `llm_memory` with the same endpoint, model, and strategy prompt,
alongside the greedy baseline. Replace both model placeholders with your installed
tool-capable model before running:

```bash
uv run sixnimmt arena --players-file examples/arena-llm-memory-players.json \
  --games 1 --seed 1234 --decision-timeout 130 --communication \
  --trace-dir traces/memory-comparison
```


### Model interaction traces

With `--trace-dir`, LLM seats automatically write a privileged
`<match-log-stem>.model.jsonl` sidecar, separate from game events and action
records. The `llm_memory` adapter adds its private notebook field to model tools
only; engine actions carry no notebook or reasoning fields.

Each model request has a `request` record written before sending, a `response`
or `provider_error` record when it returns, and an `action_parsed` or
`parse_error` record after interpreting its tool call. Malformed response decoding
produces `response_parse_error`. Repair requests are separate attempts under the
same `decision_id`, each with a unique `request_id`. The trace includes match,
player ID/display name, hand/play, view ID/version, timestamps, endpoint, prompt
hash, observation format version, request settings, HTTP status, provider request ID when available, and
response latency. Late decisions can append diagnostics after a match has ended;
`action_parsed` means the adapter parsed an action, not that the engine accepted
it. Use the game/action logs to determine acceptance or arena timeout.

`request.payload` contains the messages, tools, and generation settings.
`response.body` contains the complete decoded response body as a string; parse
it as JSON to query fields such as `choices[0].message.reasoning_content` when
returned. Raw bodies preserve unknown provider fields, reasoning, ordinary text,
tool calls, finish reasons, and usage without relying on the SDK's schema.
Error bodies are preserved too. Authentication headers are not recorded, and the
configured API key is redacted if echoed in a response. These files contain
private observations and should be treated as privileged experiment data.

Use per-seat `options.provider_options` for endpoint-specific JSON body fields
that enable or control reasoning. For example, an endpoint that supports
`reasoning_effort` can receive `{"reasoning_effort": "low"}`. Consult that
endpoint's documentation for supported fields and values; the adapter does not
translate between provider dialects. Standard options, messages, tools, streaming,
and credentials cannot be overridden through this dictionary. Credentials belong
in the configured environment variable, never in provider options.

Capture does not request additional explanations. It records reasoning only when
the endpoint returns it. Non-streaming responses are captured on completion;
if the process exits with requests outstanding, their `request` records can
remain without a corresponding response.
