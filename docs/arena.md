# Running arenas

The `arena` command compares strategies across changing opponent lineups and
prints readable results. It plays 100 four-player games by default, using the
reference strategy catalogue. Results remain in memory unless an output directory
is supplied.

```bash
uv run sixnimmt arena --games 10
```

`--games` is the exact number of random-opponent games for each selected player
count. For example, `--games 10 --player-count 3 --player-count 6` requests 20
games. Player counts from two through ten are supported. Lineup rotations use
the requested game budget; they do not multiply it.

Add `--output-dir runs/first-comparison` to save the evidence and reports. Without
that option, the command creates no run directory or output files.

See [Comparing strategies](comparisons.md) for controlled lineups, replacement
comparisons, population analysis, and reanalysis of saved results. The
[bot evaluation report](bot-evaluation.md) explains the evaluation questions.

## Configuration

The default run needs no configuration file. Use one optional JSON file to
choose strategies and any additional settings. Its `catalogue` array contains
the strategy configurations:

```json
{
  "catalogue": [
    {"bot": "random", "label": "Random", "family": "card_order"},
    {"bot": "lowest_fitting_card", "label": "Lowest fitting card", "family": "board_and_hand"}
  ]
}
```

```bash
uv run sixnimmt arena --config examples/arena.json \
  --games 10 --seed 1234 --output-dir runs/custom-comparison
```

Each entry defines a strategy that can fill a seat. The arena samples lineups
from these entries, allowing multiple copies of the same configuration. Array
positions do not prescribe seats. Labels and strategy metadata stay in the
harness; bots see anonymous player names.

`bot` names a registered [baseline or composed strategy](bots.md),
[`simulation` or `model_based_bait`](uncertainty-bots.md), or
[`llm` or `llm_memory`](llm-players.md). `options` are validated by that strategy's
options model; unknown keys fail validation. Baseline heuristics accept no
options. `label` is an optional report name, and `family` groups related
strategies for population analysis. Give different parameter settings of the
same bot distinct `key` values. Defaults and nested options are resolved and
recorded before execution.

The [baseline catalogue](../examples/arena-baseline.json) contains
eight inexpensive strategies. Model and probabilistic examples are linked from
their guides. Keep credentials in the configured environment variables;
catalogue options are included when run artifacts are saved.

## CLI reference

```bash
uv run sixnimmt arena --help
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--config` | Built-in settings | One JSON file containing strategies and optional lineup, execution, or analysis settings |
| `--games` | 100 | Random-opponent games per selected player count |
| `--player-count` | 4 | Players per game, from 2 to 10; repeat for multiple sizes |
| `--controlled-games` | 0 | Additional games per selected controlled lineup |
| `--seed` | 66 | Root seed |
| `--output-dir` | Unset | Save all run artifacts in this new directory; otherwise keep results in memory |
| `--trace` | Off | Save detailed logs in `traces/`; requires `--output-dir` |
| `--json` | Off | Print the full structured report as pretty-printed JSON |
| `--animation` / `--no-animation` | On | Show game progress and the game/analysis animations in interactive terminals |
| `--communication` | Off | Explicit commitment and messaging |
| `--scheduler` | Mode-dependent | `sequential` in classic, `round_robin` in communication |
| `--concurrency` | 1 | Maximum simultaneous games |
| `--backend` | `thread` | Thread workers or spawned process workers |
| `--match-action-limit` | 10,000 | Attempt limit across a game |
| `--play-action-limit` | Unset classic; 200 communication | Attempt limit across all seats within one play |
| `--decision-rejection-limit` | 8 | Consecutive rejections within one offered decision |
| `--decision-timeout` | Unset | Seconds allowed for a bot call |
| `--max-abandoned-decisions` | Four times concurrency | Bound on timed-out calls still running |
| `--stop-on-failure` | Off | Stop submitting games after failure; drain submitted work |

Explicit command-line settings override the corresponding configuration-file settings.
At least one random, controlled, or replacement comparison game must be requested.
Piped output and `--json` suppress the animation automatically.
`--json` alone prints the full report without saving files.
The bull-and-card animation runs during games; a statistics animation follows
while the analysis and reports are prepared. `--no-animation` disables both.

Classic sequential scheduling can offer one seat until it commits. Communication
requires round-robin scheduling to avoid starving other seats. Both policies
skip committed seats and prioritize a required row choice. Rejected attempts
count toward arena limits. Accepted actions consume the separate engine budget
where applicable. A final action that finishes a game wins over a limit reached
by that action.

## Results and failures

The command prints formatted strategy tables with percentages, compact confidence
intervals, average scores, and completion counts. It also identifies the run's
context and, when supplied, the output directory. With `--output-dir`,
`report.md` contains the main comparison and
collapsed previews of exploratory results; `analysis.json.gz` retains the full
structured analysis. `plan.json` holds the complete schedule and settings,
`results.jsonl` preserves returned outcomes, and `manifest.json` records execution
status and provenance. Without `--output-dir`, these results stay in memory and
the command only prints them. Full traces require both `--trace` and `--output-dir`.

| Outcome | Typical cause |
| --- | --- |
| `finished` | Normal game termination |
| `abandoned` | Play or match attempt limit |
| `forfeited` | Repeated illegal proposals reach the rejection limit |
| `failed` | Bot exception, construction failure, malformed return, or timeout |

Only finished games have competitive scores. Reports show completion coverage,
missing outcomes, and uncertainty alongside win and acceptable-finish credits.
See [Comparing strategies](comparisons.md) for tie conventions and denominators.

Invalid configuration stops before execution. Returned bot failures remain
ordinary result records. Fatal worker, recording, or scheduler errors stop new
submissions and preserve available results. When an output directory was supplied,
errors identify the saved evidence there.
Submitted games without returned outcomes remain visible, as do unstarted games.
Bot `stats()` failures are recorded separately and do not change a game outcome.

## Timeouts and concurrency

Use process workers for CPU-heavy comparisons and threads when bots mostly wait
for model responses. Each game advances one bot decision at a time.

```bash
uv run sixnimmt arena --config examples/arena-baseline.json \
  --games 100 --seed 1234 --backend process --concurrency 4 \
  --output-dir runs/baseline-comparison
```

Every game constructs fresh bot instances. Planned assignments and seeds remain
the same across backends and worker counts when timing limits do not intervene.
The parent collects compact results as workers finish and saves them when an
output directory is supplied. Process startup adds overhead,
so short runs may be faster with threads.

Custom factories and option models must be importable and picklable for process
execution. Resolved definitions go to workers once; bot instances need not be
picklable. Live observer callbacks require the thread backend. Python scripts
using processes must protect their entry point with `if __name__ == "__main__"`;
use a script or the CLI instead of an interactive interpreter or notebook cell.

Without a deadline, a bot runs inline on its match worker. With a deadline, its
call runs on a daemon thread. A timeout finishes the failed game and discards
late results, but cannot cancel arbitrary Python or provider work. Exceeding
the abandoned-call bound stops submission. The count and limit are shared across
process workers. A late call that returns releases its active slot; its action
never enters a later game. Statistics are skipped for a timed-out seat while its
call may still mutate the bot.

Set both arena deadlines and provider request timeouts for model runs.
`stop_on_failure` drains submitted games; without deadlines, that can wait
indefinitely. Privileged observer callbacks must be thread-safe and must never
feed hidden state back to bots.

## Python configuration

The comparison API uses `LineupConfig`, `build_arena_plan`, and `run_plan`;
see the [Python guide](python-api.md) and [comparison examples](comparisons.md).
The lower-level `run_arena` API remains useful when a script needs one fixed
ordered lineup:

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

`PlayerConfig` describes a fixed seat and accepts `display_name` and
`agent_metadata`; it differs from a catalogue entry. `run_arena` preserves its
fixed-seat aggregates and seed scheme. In thread mode it validates the first
lineup eagerly; failure to construct the first process lineup is also fatal.
Later construction failures become returned match failures.

Keep three lower-level configuration layers distinct:

| Model | Responsibility |
| --- | --- |
| `GameRules` | Published game shape, player bounds, target score |
| `MatchProtocol` | Communication, information policy, fixed-hand termination, per-player action budget |
| `RunConfig` | Scheduling, attempt limits, deadlines, backend, concurrency |

The fixed game shape is 104 cards, ten cards per hand, four rows, and capacity
five. Use the configuration file or Python for protocol fields without individual CLI
flags, such as fixed-hand termination or hidden message existence.

Sources: [configuration](../src/sixnimmt/arena/config.py),
[planning](../src/sixnimmt/arena/planning.py),
[execution](../src/sixnimmt/arena/planned.py), and
[CLI](../src/sixnimmt/cli.py).
