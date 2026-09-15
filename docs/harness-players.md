# External harness players

Run Codex and Claude Code in their own visible terminals, connect them to game
seats over MCP, and watch them play alongside built-in bots. The same table can
also supervise headless commands, including Codex and Claude Code.

## Watch native agents play

From the repository root:

```bash
uv sync --all-groups
uv run sixnimmt table --seat codex --seat claude --seat lowest_fitting_card \
  --hands 1 --output-dir runs/watched-table
```

The output directory must be new. The controller prints a launch script for each
native seat. Run each script in a separate terminal, for example:

```bash
runs/watched-table/seats/player_1/launch.sh
```

Each script opens the actual harness in its private game workspace, loads its MCP
configuration, and supplies the starting prompt. Complete any native trust or
tool-approval prompts there. The agent calls `get_game_info`, then waits in
`play` to mark its seat ready. Once every external seat is ready, the controller
asks you to start the game. Use `--auto-start` to start immediately at that point.

Watch the agents' work in those terminals. The controller animates the actual
board and scores as the game progresses. It does not launch terminal windows
automatically. A native UI that supports the same workspace and MCP settings can
be attached manually; the generated launchers target the CLIs.

Add `--communication` for table/direct messages, revised selections, and explicit
commitment. Omit `--hands` to play until a completed hand brings someone to 66
points. A table needs two to ten seats, supplied through `--seat` or a
configuration file.

Press Ctrl-C in the controller to stop a running match. This records an
`abandoned` result with reason `operator_stop` and wakes waiting agents. Their
native processes and conversations remain open. Events and actions are saved
under the table directory and can be replayed without calling a model:

```bash
uv run sixnimmt replay runs/watched-table/traces/table.jsonl
```

## Watch the live game

Interactive terminals show card reveals, placements, highlighted row captures,
and each player's score as total (banked + this hand). Cards stay face-down on
the board until the game reveals them. Public table messages appear when
communication is enabled. Each seat also shows whether it is deciding,
retrying, accepted, or failed, with elapsed time and the applicable decision deadline.

The animation consumes actual game events on a separate output thread. Its
bounded frame queue catches up when bots play quickly; animation does not add a
delay to a player's decision. Redirected output uses plain board and activity
updates.

An **operator commentary** panel is enabled by default. It can contain private
card analysis or planned moves. Headless Codex and Claude sessions stream
assistant text, tool activity, and reasoning summaries when their CLI exposes
them. Managed proposals also request a short explanation of the proposed move,
shown when the completed response arrives. This provides useful commentary even
when a CLI emits only structured output. An explanation describes a proposed
move; the seat's status separately reports whether the arena accepts it.
Existing LLM bots supply their response text after the provider response
completes; simulation and Lookahead bots report candidate values after accepted
card selections. Native sessions keep their transcripts in their own terminal or UI.
The controller does not request or reconstruct hidden reasoning.

Commentary is grouped by player, hand, play, and decision. Paragraphs, lists,
emphasis, and code blocks retain their formatting. The panel shows the requested
model and effort, labels proposed and accepted moves separately, and combines
repeated tool updates into a compact status line. Simulation comparisons show
the best candidates and chosen card, the scoring objective and units, the number
of simulated plays, and the sample count.

The live panel fits the space below the board. Completed decisions are printed
once into normal terminal scrollback, including messages from repair attempts;
scroll up to read commentary that did not fit on screen. Stored message length
and history are bounded, with truncation marked. Complete available diagnostics
remain in the bounded model trace.

| Option | Behavior |
| --- | --- |
| `--animation` / `--no-animation` | Enable live animation (default) or use plain updates |
| `--commentary` / `--no-commentary` | Show or hide the private operator panel and detailed text (default: show) |
| `--quiet` | Suppress board and activity output; keep setup and final result messages |

These display options do not disable saved traces. To watch the Sonnet, Terra,
and Lookahead example, choose a new output directory:

```bash
uv run sixnimmt table --config examples/table-sonnet-terra-lookahead.json \
  --auto-start --output-dir runs/sonnet-terra-lookahead-live
```

## Configure models and a lineup

Use one JSON file to configure a mixed table. It uses the same `catalogue`
entries and `rules`, `protocol`, and `execution` sections as the
[arena](arena.md#configuration). Set each model through its entry's
`options.model`, whether it uses the existing LLM adapter or a headless harness:

```json
{
  "catalogue": [
    {
      "key": "api-player",
      "bot": "llm",
      "options": {
        "model": "YOUR_TOOL_CAPABLE_MODEL",
        "base_url": "https://your-provider.example/v1",
        "api_key_env": "ARENA_MODEL_KEY"
      }
    },
    {
      "key": "codex-player",
      "bot": "codex-headless",
      "options": {"model": "YOUR_CODEX_MODEL"}
    },
    {
      "key": "claude-player",
      "bot": "claude-headless",
      "options": {"model": "YOUR_CLAUDE_MODEL"}
    },
    {"key": "baseline", "bot": "lowest_fitting_card"}
  ],
  "lineup": ["api-player", "codex-player", "claude-player", "baseline"],
  "seed": 66,
  "protocol": {"end_condition": "fixed_hands", "hands": 1}
}
```

Edit the model IDs, endpoint, and credential environment variable in
[the complete example](../examples/table-models.json), then run:

```bash
uv run sixnimmt table --config examples/table-models.json \
  --auto-start --output-dir runs/model-table
```

Each catalogue entry accepts `bot`, `key`, `label`, `family`, and `options`, just
as arena entries do. `key` defaults to `bot`; `label` defaults to the key. Give
different configurations of the same bot distinct keys. `lineup` lists those
keys in seat order. Repeating a key creates independent players with the same
configuration; listing an entry in the catalogue alone does not seat it.

The table uses a fixed lineup. Arena comparison settings such as game budgets,
populations, and replacement comparisons do not belong in a table file. Its
`execution` section accepts the arena's `RunConfig` fields, but requires
`backend: "thread"` and `concurrency: 1`. For example,
`"execution": {"decision_timeout_seconds": 180}` bounds each decision.
The controller saves traces in the table's output directory under `traces/`.
Omitted sections use `GameRules`, `MatchProtocol`, and `RunConfig` defaults;
the default seed is 66.

Explicit CLI options override the corresponding saved seed, protocol, and
execution settings; omitted options preserve them. Supplying `--seat` replaces
the entire configured lineup. Seat values resolve catalogue keys first, so a
smaller table can reuse the same file:

```bash
uv run sixnimmt table --config examples/table-models.json \
  --seat codex-player --seat baseline --auto-start \
  --output-dir runs/codex-table
```

### Seat shorthand

Seats appear in command-line order; each gets a distinct identity.

| Seat value | Execution and context |
| --- | --- |
| `codex`, `claude` | User-opened native session, MCP, one conversation for the match |
| `codex-headless`, `claude-headless` | Managed CLI process for each decision, final JSON output |
| A registered bot, such as `lowest_fitting_card` | Existing in-process arena bot |
| `llm:/absolute/path/options.json` | Existing LLM player with its normal options |
| `command:/absolute/path/options.json` | Managed custom command using the JSON contract below |

Registered bots accept an options file after `:`. The file contains that bot's
options object, as described in [Writing bots](bots.md) and [LLM players](llm-players.md).
Named headless seats also accept an options file after `:`. This shorthand uses
the same options object as a catalogue entry; a separate command file is not
needed when using `--config`.

For example, this runs a mixed table:

```bash
uv run sixnimmt table --seat codex --seat claude-headless --seat lowest_card \
  --hands 1 --communication --memory --output-dir runs/mixed-table
```

### Headless options

Both headless harnesses need the corresponding installed and authenticated CLI.
They accept these options; unknown keys are rejected:

| Option | Default | Behavior |
| --- | --- | --- |
| `model` | Unset | Requested model ID, passed to the harness's `--model` flag; when omitted, the CLI chooses its configured default |
| `reasoning_effort` | Unset | Requested thinking effort for every invocation, including repairs; omitted or null preserves normal client defaults |
| `command` | `["codex"]` or `["claude"]` | Optional executable and argument prefix |
| `timeout_seconds` | `--managed-timeout` (120 seconds) | Positive deadline for a managed decision, including format repair |
| `max_output_bytes` | 1,048,576 | Positive output size limit |

`options.model` takes precedence over the harness's configured default. Use this
field to select models for headless seats; the controller supplies the required
execution and output-format arguments. Native `codex` and `claude` seats retain
their own model configuration and accept no nonempty options.

Set `reasoning_effort` alongside `model` in the catalogue entry. For example,
these entries request high effort for Sonnet and Terra:

```json
{
  "catalogue": [
    {
      "key": "sonnet",
      "bot": "claude-headless",
      "options": {"model": "sonnet", "reasoning_effort": "high"}
    },
    {
      "key": "terra",
      "bot": "codex-headless",
      "options": {"model": "gpt-5.6-terra", "reasoning_effort": "high"}
    }
  ],
  "lineup": ["sonnet", "terra"],
  "protocol": {"end_condition": "fixed_hands", "hands": 1}
}
```

Codex receives `-c 'model_reasoning_effort="high"'`. Its documented common
levels are `minimal`, `low`, `medium`, `high`, and `xhigh`; the installed client's
schema also permits other nonempty model-advertised names. Sixnimmt leaves those
names and model support to Codex. See the [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

Claude receives `--effort high`. Accepted values are `low`, `medium`, `high`,
`xhigh`, and `max`. An explicit seat value also replaces
`CLAUDE_CODE_EFFORT_LEVEL` in that child process, so an inherited environment
default cannot override the table file. Native sessions and the parent shell's
environment are unaffected. The client can cap effort according to its model or
organization settings. See [Claude's effort configuration](https://code.claude.com/docs/en/model-config#adjust-effort-level)
and [environment-variable precedence](https://code.claude.com/docs/en/env-vars#precedence).

Effort names express a provider request, not a comparable token or time budget
across models. Existing LLM bots pass the same request field through
`options.provider_options.reasoning_effort` when their endpoint supports it.
Custom `command` seats reject `reasoning_effort`; configure a custom process
through its own command arguments instead.

Codex uses `exec --ephemeral --json`, a supplied output schema, and its designated
final message file. Claude uses print mode with streamed JSON, no session
persistence, and its final `structured_output` result. The controller displays
available intermediate text and tool activity; only the completed final proposal
can become a move. Each invocation has its own temporary working directory,
removed afterward.

Each decision gets a schema built from its offered action tools: the current
hand's card values, available rows, permitted message recipients and lengths,
and the configured notebook limit. With memory disabled, `memory` must be null.
The invocation's `game_info.proposal_schema` contains the same decision-specific
constraints supplied to the harness's structured-output mode. In classic mode,
the only card-selection action is `select_card`; selection commits immediately,
so a separate `commit` is unavailable. The broker and arena remain authoritative
even when a process ignores the schema.

`--memory` enables an arena-accepted notebook for external seats. Its default
limit is 4,000 characters; `--memory-max-chars` accepts 1–16,000. A proposal's
`memory: null` preserves it, an empty string clears it, and a string replaces it.
The update is applied only after the entire proposal passes game validation.
Native conversation history and files are separate from this notebook.

These profiles currently run through the single-table controller. Managed
profiles in sampled arena comparisons, persistent headless sessions, and process
scaling are not yet supported.

## The two MCP tools

The bridge implements **stateless MCP `2026-07-28` over stdio**. The live arena
broker owns game state, offers, deadlines, and receipts independently of the
bridge process. A random credential authorizes one seat; every game tool also
requires its explicit `session_id`.

| Tool | Result |
| --- | --- |
| `get_game_info(session_id)` | Seat identity, rules, instructions, proposal schema, and notebook configuration. Does not mark the seat ready or reveal a deal. |
| `play(session_id, proposal=None)` | Enter/recover with no proposal; otherwise submit immediately. Wait for a decision or terminal result and return the relevant receipt. |

There is no separate submission-status or turn-wait tool. After choosing a move,
the agent calls `play` with a proposal. The arena receives it immediately and
continues running while that MCP call remains pending. The call completes when
the seat must act again, receives a correction, or the match ends.

Every response contains `status`, `offer`, `receipt`, and `result`:

| Status | Meaning |
| --- | --- |
| `decision` | An unsubmitted offer is available. Inspect its filtered view, observation, action schemas, rejection feedback, notebook, and deadline. |
| `terminal` | The match ended. `result` contains its outcome, public scores/winners, safe reason, and this seat's final view. |
| `wait_expired` | The configured delivery wait ended. Recover with `play(session_id)`; this does not reset a decision deadline. |
| `already_waiting` | Another delivery wait for this seat remains active. No new proposal was staged. |

Schema, identity, stale-decision, and conflicting-retry errors are immediate tool
errors. Illegal game moves receive an arena rejection receipt and a corrective
offer, unless the arena's rejection limit ends the match.

Use the returned offer's identifiers to construct a proposal. All seven fields
are required; this example assumes communication mode and card 42 in hand:

```json
{
  "protocol_version": 1,
  "session_id": "session-example",
  "decision_id": "decision-example",
  "submission_id": "unique-submission-example",
  "view_id": "view-example",
  "actions": [
    {"type": "select_card", "card": 42},
    {"type": "commit"}
  ],
  "memory": null
}
```

The action types, filtered observations, and action hints match the existing LLM
players. The broker accepts one to eight operations, counting a non-null notebook
update as one. It supplies action metadata; callers cannot supply `action_id`,
`from_view`, or `expected_view_version`. The engine still validates move legality
and the arena preflights the entire batch before publishing it.

Proposals are limited to 128 KiB. Eight proposal validation errors within one
offered decision fail the session; arena game rejections have their own limit.
Each seat retains at most 10,000 submissions through the match and terminal
recovery period.

### Interrupted calls and retries

Call `play(session_id)` after a lost response or bridge restart. It returns the
same current unsubmitted offer, including its original deadline, or waits beyond
a move already submitted. Alternatively retry the exact saved proposal with its
original `submission_id`. A retry never applies another copy of that move.

A proposal call returns that submission's receipt; a no-proposal call returns
the latest receipt. Receipt and offer IDs may therefore refer to different
decisions. A `staged` receipt is not acceptance: it later settles as `accepted`,
`rejected`, or `failed`, including for the final move of the game.

Cancelling an MCP request releases its delivery wait. It does not retract a
staged move. Operator stop or decision expiry can prevent an unclaimed move from
being published; a move already claimed by the arena receives a settlement.
Only one delivery wait per seat is allowed.

## Managed command contract

For a custom process, use `"bot": "command"` and put the following options in
its catalogue entry. `command` is required for custom processes:

```json
{
  "command": ["/absolute/path/to/python", "/absolute/path/to/player.py"],
  "timeout_seconds": 120,
  "max_output_bytes": 1048576
}
```

The command runs without a shell. It reads one JSON object from stdin containing
`game_info` and `offer`, then writes exactly one complete proposal object to
stdout and exits successfully. Stderr is diagnostic output. Duplicate JSON keys,
partial output, code fences, and additional stdout text are rejected.

Managed output may add an `explanation` field to the seven-field game proposal:

```json
{
  "protocol_version": 1,
  "session_id": "session-example",
  "decision_id": "decision-example",
  "submission_id": "unique-submission-example",
  "view_id": "view-example",
  "actions": [{"type": "select_card", "card": 42}],
  "memory": null,
  "explanation": "This card fits the current row and keeps my lower cards available."
}
```

The instructions request one or two concise sentences grounded in the current
game situation, with a maximum of 1,000 characters. This is a short decision
summary for the operator. It does not ask for detailed private deliberation.
Codex and Claude's strict output schemas require the field but allow `null`.
Existing custom commands can omit it; null, empty, or whitespace-only values
produce no commentary. A wrong type or excessive length triggers the same repair
path as other malformed proposal fields.

The controller validates and saves the explanation with the proposal, then
removes it before submitting game operations to the broker. It never becomes a
table message or notebook entry, uses no action or memory budget, and requires
no additional model call. It can expose private cards or plans, so it appears
only in operator commentary and the private model trace. The native MCP
`play` contract remains the seven-field proposal shown above; it does not accept
`explanation`.

The controller permits one format/protocol repair using the same offer and
schema. Error feedback identifies unavailable action types and the allowed
alternatives, or the invalid fields or memory setting. When a proposal was
parsed, feedback includes that rejected proposal or a bounded excerpt. Both
attempts share the original work deadline. Requests, proposals, and both failed
attempts are retained in the private model trace. Game rejections follow the
arena's normal corrective-decision loop. Process failure or timeout fails the
match; no fallback move is invented. The driver bounds output and terminates/reaps
its process group on success, failure, or cancellation.

## Timing and compatibility

| Setting | Default |
| --- | --- |
| `--setup-timeout` | 600 seconds for external seats to enter `play`; no decision clock runs yet |
| `--decision-timeout` | Unset for watched tables; supply seconds to bound every decision |
| `--managed-timeout` | 120 seconds for a managed decision, including format repair; a profile can override it |
| `--wait-timeout` | 600 seconds per pending `play`; independent of decision time |
| `--retain-seconds` | 30 seconds of read-only terminal recovery before closing the controller |
| `--match-action-limit` | 10,000 arena attempts |

Generated client timeouts exceed the broker wait timeout by 60 seconds. Codex
requires both `mcp_2026_07_28` and the per-server
`CODEX_MCP_PROTOCOL_VERSION=2026-07-28` opt-in. Claude uses
`MCP_SDK_GENERATION=v2`, `MCP_PROTOCOL_NEGOTIATION=auto`, and
`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS=0`. Launch scripts supply these settings.
Legacy MCP initialization receives an explicit unsupported-version response.
The terminal recovery window also applies after operator stop; a second Ctrl-C
closes it immediately. Tables with only managed/in-process seats skip retention
because they have no attached MCP listener.

Local discovery of the actual bridge was verified with Codex CLI **0.153.4** and
Claude Code **2.1.261** on 2026-09-15, without making model requests. Offline tests
exercise complete games, the MCP wire protocol, replay, cancellation, and process
cleanup. Full games in native interfaces and long-call continuation with live
models still need a manual smoke test; native UI attachment is not yet verified.

## Information and artifacts

The broker passes only the arena's player-filtered `MatchView`. Opponents' hands,
hidden selections/messages, and deal seeds are not supplied through game tools.
The controller's board uses a public spectator view. Its separately labelled
operator commentary is privileged and can reveal a model's own cards or plans;
use `--no-commentary` to hide it. Seat bundles contain private connection
configuration, instructions, and a starting prompt; credentials are kept out of
prompts and process arguments.

This is a trusted local setup. Native harnesses retain their own filesystem and
tool permissions; private directories do not isolate agents running as the same
OS user. Keep their game analysis within their assigned workspace. Controller
traces contain privileged state and are intended for the operator.

The output directory contains `seats/`, `traces/`, and a public `result.json`.
Traces record accepted game events, attempted actions, timing, seat metadata,
and final statistics, including notebook settings and shared instruction/schema
versions. Managed sessions also write `traces/table.model.jsonl`: filtered
decision requests, proposal schemas, streamed output, final proposals, repair
feedback, decision explanations, and bounded stdout/stderr diagnostics on failure. These records include
seat, client, requested model, invocation, and decision identifiers. Known
credential values are redacted; the trace still contains private gameplay and
model output. It remains available with `--quiet` or `--no-commentary`.

The controller does not collect native transcripts. Provider-served model/version
and complete usage provenance, connection-status diagnostics, and credential
rotation are future work. See [model diagnostics](traces.md#model-diagnostics)
for record types and their relationship to accepted game events.

When supplied, a headless seat's requested model and effort are recorded in
`agent_metadata.bot_options.model` and
`agent_metadata.bot_options.reasoning_effort`, and in managed model-trace records.
These fields record configuration; they do not verify which underlying model,
version, or effective effort the provider actually served.
