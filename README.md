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

Run the command-line arena with two random bots:

```bash
uv run sixnimmt arena --players random random --games 10000 --seed 1234
```

`--players` also accepts repeated options, such as `--players random --players random`. The `--match-action-limit` (also accepted as `--max-actions-per-match`) defaults to 10,000 attempts, including rejected actions.

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

Register a strategy by adding a `BotSpec(name, build, deterministic, metadata)`
to `arena.bots.REGISTRY`. `build(seed)` creates a fresh bot for each match.
Declare reproducibility honestly: a seeded deal does not reproduce an external
model's answers. Optional `stats()` returns opaque JSON data for the manifest;
missing or failed statistics never change a match outcome.

`run_match` and `run_arena` accept `GameRules`, `MatchProtocol`, and `RunConfig`.
An optional `observer(state, events)` receives setup and every appended batch.
It is privileged instrumentation with authoritative state and must be thread-safe
when matches run concurrently.

## Traced experiments

```bash
uv run sixnimmt arena --players random random greedy --games 1000 --seed 1234 \
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
