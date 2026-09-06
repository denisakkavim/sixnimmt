# Running arenas

## Player configuration

The CLI reads a JSON array of two to ten player objects:

```json
[
  {"bot": "random", "display_name": "Alice", "options": {}, "agent_metadata": {"group": "baseline"}},
  {"bot": "greedy", "display_name": "Bob"}
]
```

`bot` names a registered strategy: `random`, `greedy`, `llm`, or `llm_memory`.
`options` are validated by that strategy's options model; unknown keys fail
validation. Random and greedy accept no options. Names default to `Player 1`,
`Player 2`, etc. IDs remain `player_1`, `player_2`, etc. Seat metadata is recorded
for experiment analysis. It overrides registry metadata, while the arena supplies
the `bot_options` metadata field itself. Options and metadata must not contain
credentials because traces record them.

## Python configuration

```python
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.arena.runner import RunConfig, run_arena
from sixnimmt.engine.rules import MatchProtocol

result = run_arena(
    players=[PlayerConfig(bot="random"), PlayerConfig(bot="greedy")],
    games=10,
    seed=1234,
    protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
    config=RunConfig(concurrency=2),
)
print(result.finished, result.players)
```

Keep three configuration layers distinct:

| Model | Responsibility |
| --- | --- |
| `GameRules` | Published game shape, player bounds, target score |
| `MatchProtocol` | Communication, information policy, hand-based termination, per-player action budget |
| `RunConfig` | Scheduling, attempt limits, deadlines, concurrency, tracing |

The fixed shape is 104 cards, ten cards per hand, four rows, and capacity five;
alternative values are rejected. Use the Python API for protocol fields that
the CLI does not expose, such as fixed-hand experiments or hidden message existence.

## CLI reference

```bash
uv run sixnimmt arena --help
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--players-file` | Required | JSON lineup file |
| `--games` | Required | Positive number of matches |
| `--seed` | 66 | Root seed |
| `--communication` | Off | Explicit commitment and messaging |
| `--scheduler` | Mode-dependent | `sequential` in classic, `round_robin` in communication |
| `--concurrency` | 1 | Concurrent match workers |
| `--trace-dir` | Unset | New directory for logs and manifest |
| `--match-action-limit` | 10,000 | Attempt limit across a match |
| `--max-actions-per-match` | 10,000 | Older spelling; explicit `--match-action-limit` takes precedence |
| `--play-action-limit` | Unset classic; 200 communication | Attempt limit across all seats within one play |
| `--decision-rejection-limit` | 8 | Consecutive rejections within one offered decision |
| `--decision-timeout` | Unset | Seconds allowed for a bot call |
| `--max-abandoned-decisions` | Four times concurrency | Bound on timed-out calls still running |
| `--stop-on-failure` | Off | Stop submitting matches after a failure; drain started work |

Classic sequential scheduling can repeatedly offer one seat until it commits.
Communication therefore requires round-robin scheduling to avoid starving
other seats. Both policies skip committed seats and prioritize a required row
choice. Attempts count toward arena limits even if rejected. Accepted actions
consume the separate engine budget where applicable. A final action that
finishes a match wins over a limit reached by that action.

## Outcomes and failures

| Outcome | Typical cause |
| --- | --- |
| `finished` | Normal game termination |
| `abandoned` | Play or match attempt limit |
| `forfeited` | Repeated illegal proposals reach the decision rejection limit |
| `failed` | Bot exception, malformed return, or decision timeout |

Only finished matches contribute scores, sole wins, and ties. Report unfinished
outcomes alongside win rates to avoid hiding a strategy's failure rate. Track
requested, started, and completed counts when submission stops early.

Invalid configuration and failure to construct the initial lineup stop the run.
Later bot construction failures become match failures. Fatal tracing or scheduler
errors stop the run with `ArenaError`; a completed manifest may contain partial
run results. Bot `stats()` failures are recorded separately and do not change
the game outcome.

## Timeouts and concurrency

Without a deadline, a bot runs inline on the match worker. With a deadline, its
call runs on a daemon thread. Timeout finalizes the failed match and discards
late results, but cannot cancel arbitrary Python or provider work. Exceeding the
abandoned-call bound stops submission. Statistics are skipped for timed-out
seats because a late call could still mutate their state.

Set both the arena deadline and provider request timeouts for model experiments.
`stop_on_failure` drains started matches; without deadlines, that can wait
indefinitely. An `observer(state, events)` callback must be thread-safe under
concurrency and must not feed privileged state back into bot decisions.

Sources: [configuration](../src/sixnimmt/arena/config.py),
[players](../src/sixnimmt/arena/players.py),
[runner](../src/sixnimmt/arena/runner.py), and
[CLI](../src/sixnimmt/cli.py).
