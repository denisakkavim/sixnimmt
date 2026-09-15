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

Watch the agents' work in those terminals. The controller prints the public
board and scores as the game progresses. It does not launch terminal windows
automatically. A native UI that supports the same workspace and MCP settings can
be attached manually; the generated launchers target the CLIs.

Add `--communication` for table/direct messages, revised selections, and explicit
commitment. Omit `--hands` to play until a completed hand brings someone to 66
points. Between two and ten explicit `--seat` options are required.

Press Ctrl-C in the controller to stop a running match. This records an
`abandoned` result with reason `operator_stop` and wakes waiting agents. Their
native processes and conversations remain open. Events and actions are saved
under the table directory and can be replayed without calling a model:

```bash
uv run sixnimmt replay runs/watched-table/traces/table.jsonl
```

## Choose a lineup

Seats appear in command-line order; each gets a distinct identity.

| Seat value | Execution and context |
| --- | --- |
| `codex`, `claude` | User-opened native session, MCP, one conversation for the match |
| `codex-headless`, `claude-headless` | Managed CLI process for each decision, final JSON output |
| A registered bot, such as `lowest_fitting_card` | Existing in-process arena bot |
| `llm:/absolute/path/options.json` | Existing LLM player with its normal options |
| `command:/absolute/path/profile.json` | Managed custom command using the JSON contract below |

Registered bots accept an options file after `:`. The file contains that bot's
options object, as described in [Writing bots](bots.md) and [LLM players](llm-players.md).
Named headless seats also accept a command profile after `:` to select executable
arguments and execution limits.

For example, this runs a mixed table:

```bash
uv run sixnimmt table --seat codex --seat claude-headless --seat lowest_card \
  --hands 1 --communication --memory --output-dir runs/mixed-table
```

Both headless profiles need the corresponding installed and authenticated CLI.
Codex uses `exec --ephemeral`, a supplied output schema, and its designated final
message file. Claude uses print mode, no session persistence, and its
`structured_output` result. Intermediate transcript text is never treated as a
move. Each invocation has its own temporary working directory, removed afterward.

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

A profile is a strict JSON object:

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

The controller permits one format/protocol repair using the same offer and safe
error feedback. Both attempts share the original work deadline. Game rejections
follow the arena's normal corrective-decision loop. Process failure or timeout
fails the match; no fallback move is invented. The driver bounds output and
terminates/reaps its process group on success, failure, or cancellation.

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
The controller display uses a public spectator view. Seat bundles contain private
connection configuration, instructions, and a starting prompt; credentials are
kept out of prompts and process arguments.

This is a trusted local setup. Native harnesses retain their own filesystem and
tool permissions; private directories do not isolate agents running as the same
OS user. Keep their game analysis within their assigned workspace. Controller
traces contain privileged state and are intended for the operator.

The output directory contains `seats/`, `traces/`, and a public `result.json`.
Traces record accepted game events, attempted actions, timing, seat metadata,
and final statistics, including notebook settings and shared instruction/schema
versions. The controller does not collect native transcripts or
claim to expose hidden reasoning. Detailed model/version/usage provenance,
connection-status diagnostics, and credential rotation are future work.
