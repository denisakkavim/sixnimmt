# Running games

Use one command, `sixnimmt play`, for fixed lineups, sampled experiments, and
watched games. One `RunSettings` model controls all three. Every run builds an
explicit plan, executes its jobs, analyses the returned outcomes, and prints a
report. Results stay in memory unless an output directory is supplied.

```bash
# Sample changing lineups from the reference strategy catalogue.
uv run sixnimmt play --games 10 --player-count 4

# Repeat an exact ordered lineup and watch the games.
uv run sixnimmt play --seat random --seat lowest_fitting_card --games 2 --watch --hands 1
```

An explicit lineup defaults to one game. Without one, the default is 100 games
at four players. For sampled schedules, `--games` is the exact budget per selected
player count: `--games 10 --player-count 3 --player-count 6` requests 20 games.
Seat rotations use that budget; they do not multiply it. Fixed lineups preserve
seat order and construct fresh players for each game.

Player counts from two through ten are supported. `--watch` selects live
presentation; it does not select another runner or artifact format. See
[external harness players](harness-players.md) for native and managed seats, and
[comparing strategies](comparisons.md) for controlled lineups, replacement
comparisons, population estimates, and reanalysis.

## Configuration

Use one optional JSON file for strategies and run settings:

```json
{
  "catalogue": [
    {"key": "random", "bot": "random", "label": "Random"},
    {"key": "low", "bot": "lowest_fitting_card", "label": "Lowest fitting card"}
  ],
  "lineup": ["random", "low"],
  "games": 2,
  "seed": 1234,
  "protocol": {"end_condition": "fixed_hands", "hands": 1},
  "display": {"watch": true},
  "recording": {"output_dir": "runs/fixed-lineup", "trace": true}
}
```

```bash
uv run sixnimmt play --config settings.json
```

`lineup` lists catalogue keys in seat order; repeating a key creates independent
copies of that player. `--seat` replaces the lineup and accepts a catalogue key,
a bot name, or `bot:OPTIONS.json`. Without `lineup` or `--seat`, the catalogue
supplies a sampled schedule, allowing multiple copies of a strategy in one game.
Sampled experiments require anonymous player names. Fixed lineups can expose
their labels according to `protocol.anonymise_display_names`.

| Section | Responsibility |
| --- | --- |
| `catalogue` | Available bot configurations: `bot`, `key`, `label`, `family`, `options` |
| `lineup` | Optional fixed ordered catalogue keys |
| `games`, `player_counts`, `seed` | Game budget, sampled table sizes, and root seed |
| `populations`, `compositions`, `comparisons` | Optional experimental design; see the comparison guide |
| `rules` | Published game shape, player bounds, and target score (`GameRules`) |
| `protocol` | Communication, information policy, termination, and accepted-action budget (`MatchProtocol`) |
| `execution` | Scheduling, attempt limits, deadlines, backend, and concurrency (`RunConfig`) |
| `session` | External readiness, managed invocation, retention, and notebook policy (`SessionOptions`) |
| `display` | `watch`, `animation`, `commentary`, and `quiet` (`DisplayOptions`) |
| `recording` | `output_dir` and `trace` (`RecordingOptions`) |
| `analysis` | Report filters, cutoffs, resampling, and evidence labels (`AnalysisSpec`) |

`bot` names a registered [strategy](bots.md), a [probabilistic bot](uncertainty-bots.md),
an [LLM player](llm-players.md), or an [external seat](harness-players.md).
External seats currently require an explicit fixed lineup. Strategy `options`
are validated by their own model; unknown keys fail validation. Baseline heuristics
accept no options. `key` defaults to the bot name, so give parameter variants
distinct keys. `label` names a configuration in reports; `family` groups related
strategies for population analysis.

Counts reject booleans, quoted numbers, and fractional values. Durations must be
finite and in range; unknown configuration fields and finite-vocabulary values
are rejected. Validation applies to Python and JSON inputs. Fixed lineups reject
conflicting sampling or seat-permutation settings. Session, presentation, and
recording settings do not change the frozen experimental design or its seeds.

The [baseline catalogue](../examples/arena-baseline.json) contains eight inexpensive
strategies. Keep credentials in the configured environment variables. Saved plans
include resolved strategy options and implementation identity; external command
arguments are replaced with a construction fingerprint. Reusing a plan with
private command settings requires the originals, as described in the
[Python guide](python-api.md).

## CLI reference

```bash
uv run sixnimmt play --help
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--config` | Built-in settings | One JSON `RunSettings` file |
| `--seat` | Unset | Repeat for fixed seats in order; replaces the configured lineup |
| `--games` | Fixed: 1; sampled: 100 | Fixed repeats, or random-opponent games per selected player count |
| `--player-count` | Fixed: lineup length; sampled: 4 | Players per sampled game; repeat for multiple sizes |
| `--controlled-games` | 0 | Additional games per selected controlled lineup |
| `--seed` | 66 | Root seed |
| `--hands` | Unset | Finish after this many hands instead of reaching the target score |
| `--output-dir` | Unset | Save evidence and reports in this new directory |
| `--trace` | Off | Save detailed logs in `traces/`; requires an output directory |
| `--watch` | Off | Display live public game events and optional private operator commentary |
| `--json` | Off | Print the full structured report as pretty-printed JSON |
| `--animation` / `--no-animation` | On | Animate interactive output |
| `--commentary` / `--no-commentary` | On | Show or hide private operator commentary when watching |
| `--quiet` | Off | Omit board/activity output; keep launch instructions and results |
| `--communication` | Off | Explicit commitment and messaging |
| `--scheduler` | Protocol-dependent | `sequential` in classic, `round_robin` in communication |
| `--concurrency` | 1 | Maximum simultaneous games |
| `--backend` | `thread` | Thread workers or spawned process workers |
| `--match-action-limit` | 10,000 | Attempt limit across a game |
| `--play-action-limit` | Unset classic; 200 communication | Attempt limit across all seats within one play |
| `--decision-rejection-limit` | 8 | Consecutive rejections within one offered decision |
| `--decision-timeout` | Unset | Seconds allowed for a bot call |
| `--max-abandoned-decisions` | Four times concurrency | Bound on timed-out calls still running |
| `--stop-on-failure` | Off | Stop submitting games after failure; drain submitted work |
| `--auto-start` | Off | Start once all external seats are ready without operator confirmation |
| `--setup-timeout` | 600 seconds | External-seat readiness limit, before decision clocks start |
| `--managed-timeout` | 120 seconds | Managed invocation/repair budget unless overridden by seat options |
| `--wait-timeout` | 600 seconds | Maximum pending MCP `play` call |
| `--retain-seconds` | 30 seconds | Keep attached-seat terminal results available for reconnects |
| `--memory` | Off | Enable an arena-accepted notebook for external seats |
| `--memory-max-chars` | 4,000 | External notebook limit, from 1 through 16,000 |

Explicit command-line settings override their configuration-file counterparts.
Piped output and `--json` suppress animation. Display settings do not enable or
disable recording; use `recording.trace` or `--trace` explicitly. External sessions use temporary workspaces unless an output directory is supplied.
Use `--output-dir` to retain their workspaces and evidence.

Classic sequential scheduling can offer one seat until it commits. Communication
requires round-robin scheduling to avoid starving later seats. Both skip committed
seats and prioritize a required row choice. Rejected attempts count toward arena
limits. Accepted actions consume the separate engine budget where applicable.
A final action that finishes a game wins over a limit reached by that action.

## Results and failures

Every run produces the same compact `ArenaRun` and `EvaluationReport`, including
fixed games and external lineups. Only finished matches contribute competitive
scores. Reports show coverage, unfinished games, uncertainty, and the context
needed to interpret the estimates. Repeating a fixed lineup measures those
opponents and seats; it does not establish performance against a sampled population.

| Outcome | Typical cause |
| --- | --- |
| `finished` | Normal game termination |
| `abandoned` | Play or match attempt limit, or operator stop |
| `forfeited` | Repeated illegal proposals reach the rejection limit |
| `failed` | Bot exception, construction failure, malformed return, or timeout |

With an output directory, `plan.json`, `results.jsonl`, and `manifest.json` retain
factual evidence; `report.md` and `analysis.json.gz` contain derived reports.
Optional traces use the planned match ID under `traces/`, with a shared
`traces/manifest.json` index. External workspaces live at
`matches/<match_id>/seats/player_N/`. There is no separate watched-game result
format. See [Traces and replay](traces.md).

Invalid configuration stops before execution. Returned bot failures remain
ordinary results. Fatal worker, recording, or scheduling errors stop submissions
and preserve available evidence. Submitted games without returned outcomes and
unstarted games remain visible. Bot `stats()` and cleanup errors are privileged
diagnostics; they do not replace an established game outcome.

## Timeouts and concurrency

Use process workers for CPU-heavy registered strategies and threads for bots
that mostly wait for model responses. Each game advances one decision at a time.

```bash
uv run sixnimmt play --config examples/arena-baseline.json \
  --games 100 --seed 1234 --backend process --concurrency 4 \
  --output-dir runs/baseline-comparison
```

Every game constructs fresh players. Planned assignments and seeds stay the same
across backends and worker counts when timing limits do not intervene. The parent
collects compact outcomes as workers finish. Process startup adds overhead, so
short runs may be faster with threads.

Custom factories and options models must be importable and picklable for process
execution. Python process entry points require `if __name__ == "__main__"`; use a
script or the CLI instead of a notebook cell. Live callbacks and external-session
supervision require the thread backend. Watching games and attached native sessions require concurrency one; managed
seats can run concurrently on threads.

Without a deadline, a bot runs inline on its match worker. With one, its call runs
on a daemon thread. A timeout ends the failed game and discards late results but
cannot cancel arbitrary Python or provider work. Exceeding the abandoned-call
bound stops submission; its counter is shared across process workers. A returning
late call releases its slot and cannot affect a later game. Statistics are skipped
for a timed-out seat while its call may still mutate the bot.

Set both arena deadlines and provider request timeouts for model runs.
`stop_on_failure` drains submitted work, which can wait indefinitely without
deadlines. Setup and connection-retention windows are separate session policies.
Privileged callbacks must be thread-safe and must never feed hidden state to bots.

## Python configuration

```python
from sixnimmt.application import run
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.planning import CandidateConfig, RunSettings
from sixnimmt.engine.rules import MatchProtocol

result = run(
    RunSettings(
        catalogue=(CandidateConfig(bot="random"), CandidateConfig(bot="lowest_fitting_card")),
        lineup=("random", "lowest_fitting_card"),
        games=10,
        seed=1234,
        protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
        execution=RunConfig(concurrency=2),
    )
)
print(result.report.diagnostics.finished_matches)
print(result.run.results[0].scores)
```

`application.run(settings)` returns `RunResult(run, report, artifacts)`. Published
artifact paths are `None` for an in-memory run. `build_arena_plan` and `run_plan`
are lower-level integration APIs for the same execution path, not separate fixed
and sampled workflows. See the [Python guide](python-api.md).

Sources: [configuration](../src/sixnimmt/arena/config.py),
[planning](../src/sixnimmt/arena/planning.py),
[execution](../src/sixnimmt/arena/execution.py),
[application](../src/sixnimmt/application.py), and [CLI](../src/sixnimmt/cli.py).
