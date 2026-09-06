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
  --negotiation --scheduler round_robin --concurrency 8 --trace-dir traces/run-1234
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

Negotiation defaults to `round_robin`, offering uncommitted seats in rotation.
Classic defaults to `sequential`; pairing it with negotiation is refused because
one noncommitting seat could starve everyone else. Required row choices always
go directly to the awaited player. The stock schedulers skip committed seats,
so those seats cannot initiate an uncommit through these scheduling policies.

`--play-action-limit` defaults to 200 in negotiation and is unset in classic.
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

## Negotiation messaging

Set `protocol.negotiation_enabled` to `true` when creating an HTTP match to enable
explicit commitment, uncommit, table messages, and direct messages. Send a message
through `/matches/{id}/message` (or `/actions` with `type: "send_message"`):

```json
{"visibility": "direct", "to_player": "bob", "body": "Which row would you take?", "action_id": "message-1"}
```

For a table message, use `visibility: "table"` and omit `to_player`. Messages are
accepted only during selection. Self-directed messages are refused. Empty bodies
are allowed; `max_message_length` counts Unicode characters (default 2000), and
text must be representable as UTF-8.

The state view includes `messages`, `private_messages_observed`, and
`messages_omitted`. Together the first two lists retain the latest 100 eligible
messages from the current play and reset when the next play starts. Direct-message
content appears once for each participant or privileged observer. Other viewers
see only the parties when `information_policy.private_message_existence` is
`"visible"`; with `"hidden"`, their views and cursors do not change. Use `/events`
for cross-play history and ordering between content and occurrence entries.

Selections, commits, uncommits, and messages each consume one action when
`max_actions_per_play` is set. Required row choices remain available at zero
budget. Counts and remaining budgets are private to the actor, including with a
finite budget, because public budgets would expose hidden messages. New logs
record every count; older logs retain their historical selection-only accounting.

The message cap bounds response size, not server resource use: live event history
and subscriber queues remain unbounded, and subscribers can accumulate queued events. Views on both surfaces advance
incrementally as events arrive. Harnesses must still bound experiments and
abandon stalled matches. Negotiation is available through both HTTP and the arena.

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

Each decision sends fresh instructions, the player's filtered view, row penalties,
and the previous action and any rejection. There is no growing conversation or
persistent model memory. Only currently available action types are offered as
tools. Exactly one function call is accepted; the arena then regains control.
Classic selection and row choice, and negotiation messaging and commitment, use
the same path. Add `--negotiation` to enable negotiation. Views include the public
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
| `system_prompt` | Built-in game instructions | Replace the full system instructions for this seat |
| `strategy_prompt` | `""` | Seat-specific strategy and personality appended to system instructions |

Local parsing always rejects extra fields and incorrect argument types, regardless
of `strict_tools`. Well-formed illegal actions reach the engine and its existing
rejection/retry budget. Malformed responses receive bounded repair feedback.
Provider errors fail the match without SDK retries or a fallback move, making
provider failures visible in comparisons. Error bodies are excluded from recorded
failure reasons.

Set the arena's `--decision-timeout` slightly above `decision_budget_seconds`.
The budget caps time allotted to subsequent requests and discards late responses;
SDK network timeouts are not a guaranteed wall-clock cancellation. The arena's
outer deadline remains the backstop. Start with concurrency 1 for a local model.

Tracing records resolved model options, prompt version/hash, returned model
identifiers, calls, repairs, errors, token usage, and request latency per seat.
Missing usage is counted explicitly. Monetary cost and raw model transcripts are
not currently recorded. Model runs are marked non-reproducible; game event logs
still replay without calling a model. The root seed controls deals and scripted
bots, and is not sent as a claim of model determinism.


Bot implementations live together in `src/sixnimmt_server/arena/bots/`:
`random.py`, `greedy.py`, and `llm.py`. Shared contracts are in `base.py`, the
registry is in `__init__.py`, and default LLM instructions and tool schemas are
in `prompt.py`. Existing imports from `sixnimmt_server.arena.bots` still work.

Give each LLM seat its own `options.strategy_prompt` to compare strategies or
personalities, even when both seats use the same model. For example:

```json
{
  "bot": "llm",
  "display_name": "Assertive negotiator",
  "options": {
    "model": "qwen3.5:0.8b-mlx",
    "base_url": "http://localhost:11434/v1",
    "strategy_prompt": "Take calculated risks. Negotiate assertively and propose mutually beneficial deals."
  }
}
```

Use `options.system_prompt` when you want to replace the default game instructions
entirely. `strategy_prompt` is appended to whichever system prompt that seat uses.
Tool schemas and one-action validation remain enforced by code. Both resolved
prompts are recorded in the seat options, and statistics include the hash of the
combined instructions so prompt variants can be distinguished in experiments.
