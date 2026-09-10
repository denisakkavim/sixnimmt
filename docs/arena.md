# Running arenas

## Player configuration

The CLI reads a JSON array of two to ten player objects:

```json
[
  {"bot": "random", "display_name": "Alice", "options": {}, "agent_metadata": {"group": "baseline"}},
  {"bot": "lowest_fitting_card", "display_name": "Bob"}
]
```

`bot` names a registered strategy: one of the eight [baseline heuristics](bots.md#built-in-baselines-and-board-and-hand-heuristics),
[`controlled_burn`](bots.md#controlled-burn),
[`count_threshold_bait`](bots.md#count-threshold-bait), `llm`, or `llm_memory`.
`options` are validated by that strategy's options model; unknown keys fail
validation. The baseline heuristics accept no options. Names default to `Player 1`,
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
    players=[PlayerConfig(bot="random"), PlayerConfig(bot="lowest_fitting_card")],
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
| `RunConfig` | Scheduling, attempt limits, deadlines, execution backend, concurrency, tracing |

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
| `--backend` | `thread` | `thread` for shared-process workers; `process` for multiple CPU cores |
| `--trace-dir` | Unset | New directory for logs and manifest |
| `--match-action-limit` | 10,000 | Attempt limit across a match |
| `--max-actions-per-match` | 10,000 | Older spelling; explicit `--match-action-limit` takes precedence |
| `--play-action-limit` | Unset classic; 200 communication | Attempt limit across all seats within one play |
| `--decision-rejection-limit` | 8 | Consecutive rejections within one offered decision |
| `--decision-timeout` | Unset | Seconds allowed for a bot call |
| `--max-abandoned-decisions` | Four times concurrency | Bound on timed-out calls still running |
| `--stop-on-failure` | Off | Stop submitting matches after a failure; drain started work |
| `--animation/--no-animation` | On in interactive terminals | Show the animated bull pen while matches run |

Classic sequential scheduling can repeatedly offer one seat until it commits.
Communication therefore requires round-robin scheduling to avoid starving
other seats. Both policies skip committed seats and prioritize a required row
choice. Attempts count toward arena limits even if rejected. Accepted actions
consume the separate engine budget where applicable. A final action that
finishes a match wins over a limit reached by that action.

### Terminal results

While the arena runs, interactive terminals show an animated bull pen with four
rows of five card slots and bull-head totals. Each row starts with cards; more
cards arrive, and a sixth card triggers a capture and starts a fresh row.
It includes elapsed time, the requested match count, the seed, and rotating
game tips. The doodle table uses the configured player count and display names
in seat order, with a pretend card reveal rotating through every player. Missing
names use the same defaults as the results table. The card sequence is a
decorative doodle, not a live match or a
completion percentage. It works with both thread and process backends and
clears when the run ends, including on errors. Use `--no-animation` to disable
it. Redirected output and basic `TERM=dumb` terminals skip the animation.

At the end of a run, the arena prints a formatted summary with the root seed,
hand and action totals, requested/started/completed match counts, and an outcome
table. The player table keeps seat order and shows display names, player IDs,
bot strategies, sole wins, ties, total scores, and average scores per finished
match. Lower scores are better. If no matches finish, averages display as `—`.

Tables adapt to the terminal width and use color when supported. Redirected
output remains readable text without automatic ANSI colors. Set `NO_COLOR=1`
to disable color in the terminal. This replaces the previous `key=value` output.
For structured per-match data, use [`summarise` or the trace files](traces.md).

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

`run_arena` defaults to a thread pool. Use the process backend for CPU-heavy
baseline tournaments; `concurrency` is the maximum number of matches in flight
and worker processes. Each match still advances one bot decision at a time.
Threads remain useful when model calls spend most of their time waiting for I/O.

```bash
uv run sixnimmt arena --players-file examples/arena-baseline-players.json \
  --games 100 --seed 1234 --backend process --concurrency 4
```

For Python, save the following in a script and protect the entry point with
`if __name__ == "__main__"`. Workers use the `spawn` start method, so the entry
module must be importable; run process arenas from scripts or the CLI, not an
interactive interpreter or notebook cell.

```python
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.arena.runner import RunConfig, run_arena


def main() -> None:
    result = run_arena(
        [PlayerConfig(bot="random"), PlayerConfig(bot="lowest_fitting_card")],
        games=100,
        seed=1234,
        config=RunConfig(backend="process", concurrency=4),
    )
    print(result.finished, result.players)


if __name__ == "__main__":
    main()
```

Every worker constructs fresh bots for each match, including game zero. Match
and bot seeds depend on game and seat indices, so changing the worker count or
backend preserves deterministic game outcomes when timing limits do not intervene.
Workers write separate trace files; only compact match summaries and manifest
entries return to the parent. The parent aggregates scores and writes the final
manifest in game-index order. Process startup has a cost, so very short runs may
be faster with threads. `run_match` remains a local, single-match operation;
backend and concurrency settings apply to `run_arena`.

All built-in bots support process mode. Custom factories, options models, and
resolved settings must be picklable and available from importable modules.
Register them in the parent before calling `run_arena`; the resolved definitions
are supplied to workers without requiring registration to run again there.
Lambdas and local definitions are rejected before workers launch or traces are
created. Bot instances themselves need not be picklable. Live `observer` callbacks
are unsupported in process mode; use trace files or the thread backend.

Without a deadline, a bot runs inline on the match worker. With a deadline, its
call runs on a daemon thread. Timeout finalizes the failed match and discards
late results, but cannot cancel arbitrary Python or provider work. Exceeding the
abandoned-call bound stops submission. Statistics are skipped for timed-out
seats because a late call could still mutate their state.

In process mode, the abandoned-call count and active-call limit are shared across
all workers. A call that eventually returns releases its active slot. Worker
processes are reused between matches, and late actions never enter subsequent
matches. A worker crash stops submission and raises `ArenaError`; the manifest
records completed matches and the error, without counting lost results as
completed matches. Initial-lineup construction errors are also fatal, although
other process matches may already have been submitted when the error arrives.

Set both the arena deadline and provider request timeouts for model experiments.
`stop_on_failure` drains started matches; without deadlines, that can wait
indefinitely. In thread mode, an `observer(state, events)` callback must be thread-safe under
concurrency and must not feed privileged state back into bot decisions.

Sources: [configuration](../src/sixnimmt/arena/config.py),
[players](../src/sixnimmt/arena/players.py),
[runner](../src/sixnimmt/arena/runner.py), and
[CLI](../src/sixnimmt/cli.py).
