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

`--players` also accepts repeated options, such as `--players random --players random`. The optional `--max-actions-per-match` limit defaults to 10,000 and stops a match whose bot decisions do not reach a terminal state within that bound.

The command reports the root seed, aggregate hand and action counts, and per-seat wins, ties, total scores, and average scores. Wins count sole winners; ties count matches in which that seat shared the lowest score. Lower scores are better. Given the same Python runtime and arguments, it produces the same results.

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

Custom trusted bots implement `act(observation)`, returning `SelectCardAction` or `ChooseRowAction` according to `observation.decision`. An optional `observer(state, events)` callback on `run_match` or `run_arena` receives setup and every transition batch for testing or instrumentation. Unlike a bot, this callback is privileged and sees authoritative state.

## Bot trust boundary

The arena runs bots in process. A bot receives only its own hand and public row information, but it is trusted code: the arena does not sandbox it, impose a wall-clock timeout, or protect the process from blocking, excessive resource use, or malicious behavior. Only run bot implementations you trust. The action limit bounds completed bot decisions; it cannot interrupt a bot callback that never returns.

The arena currently keeps aggregate results in memory. It does not persist action logs or replay files yet.

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
and subscriber queues remain unbounded, and views fold the full visible history.
A 500-message regression test checks finite-load behavior; sustained-spam health
is not yet guaranteed. Harnesses must bound experiments and abandon stalled
matches. Negotiation is available through HTTP and the pure engine; the arena
runner still drives classic matches.
