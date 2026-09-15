# Comparing strategies

`arena` plays games across opponent lineups and prints a readable comparison.
The default run stays entirely in memory. One optional configuration file controls
the comparison; `--output-dir` saves its evidence and reports in one directory.

```bash
uv run sixnimmt arena --games 100 --player-count 6
```

This plays exactly 100 six-player games using the eleven reference strategies.
Every game draws a fresh lineup and deal and constructs fresh bots. Each seat
is sampled independently, so multiple independent copies of a strategy can
appear together. The default is 100 games with four players; any player count
from 2 to 10 is supported. Repeat `--player-count` to analyse several table
sizes separately, with the requested number of games at each size.

Games use classic rules and finish after a completed hand brings someone to
66 points. The terminal shows a formatted strategy table with win and top-half
percentages, confidence intervals, average scores, and completion counts.
Add `--output-dir runs/comparison` to save the evidence, a readable `report.md`,
and the full structured analysis in `analysis.json.gz`. Without `--output-dir`,
the command creates no files or run directory.

Interactive terminals show a bull-and-card animation and completed-game progress
while the games run, followed by a statistics animation while analysis and reports
are prepared. Use `--no-animation` to disable both. Piped output and `--json`
omit the animations automatically.

## One configuration file

Use `--config settings.json` to choose strategies or save more detailed settings:

```json
{
  "catalogue": [
    {"bot": "random", "family": "card_order"},
    {"bot": "closest_gap", "family": "board"},
    {
      "key": "burn",
      "bot": "controlled_burn",
      "family": "capture",
      "options": {"K": 5, "fallback_strategy": "closest_gap"}
    }
  ],
  "player_counts": [6],
  "games": 100,
  "seed": 1234
}
```

The catalogue lists available strategies, not fixed seats. `key` defaults to the
bot name; use distinct keys for different parameter settings. `label` optionally
sets the name in reports. The plan records resolved defaults, nested component
options, and implementation identity. Bots see anonymous seats and legitimate
game observations; report labels stay with the arena.

The same file can contain populations, controlled lineups, replacement
comparisons, game rules, execution limits, and analysis settings. Its fields
match the Python `LineupConfig` model. Explicit command options override the
corresponding file settings; omitted options preserve them.

| Option | Default | Meaning |
| --- | --- | --- |
| `--config` | Built-in reference strategies | Optional JSON strategy and arena settings |
| `--games` | 100 | Random-opponent games per selected player count |
| `--player-count` | 4 | Players per game, from 2 to 10; repeat for several sizes |
| `--controlled-games` | 0 | Additional games per selected controlled lineup |
| `--seed` | 66 | Seed for reproducible games |
| `--output-dir` | Unset | Save all evidence and reports in this new directory |
| `--trace` | Off | Also save full logs in `traces/`; requires `--output-dir` |
| `--json` | Off | Print the full structured report and artifact paths as pretty-printed JSON |
| `--animation` / `--no-animation` | On | Show game and analysis animations in interactive terminals |

Execution options such as `--backend process`, `--concurrency 4`, and
`--decision-timeout` are described in the [arena guide](arena.md#cli-reference).
Game counts are actual matches; seat balancing does not multiply them. A root
seed determines both deals and the private random streams used by bots.
`--json` without `--output-dir` prints the complete report and saves nothing.

## Controlled opponents and replacements

`controlled_games` adds games against deliberately selected lineups. The default
selection includes every homogeneous lineup and every pair of strategies at
every copy count, with identical lineups deduplicated. `controlled_coverage` can
instead select `exhaustive` coverage or `explicit` lineups supplied through
`compositions`. Set `games` to zero when only controlled or replacement games
are wanted. At least one game must be requested.

Named `uniform` and `family_balanced` populations are included in each plan.
The latter gives every strategy family equal weight, then divides that weight
among its entries. Additional `PopulationConfig` objects can define frozen
subsets or explicit positive weights. Set `population_id` to choose the
population used to draw random lineups. Retain the original population's
`members` when adding a new candidate without changing the benchmark.

A replacement comparison plays each candidate and its reference against the
same opponents, deal, seat assignment, and assigned private seeds. For example:

```python
from sixnimmt.arena.catalogue import REFERENCE_GROUP
from sixnimmt.arena.planning import LineupConfig, ReplacementComparison

settings = LineupConfig(
    player_counts=(4,),
    games=0,
    comparisons=(
        ReplacementComparison(
            comparison_id="gap_vs_high_in_groups",
            reference="highest_card",
            candidates=("closest_gap",),
            group_roster=REFERENCE_GROUP,
            games=10,
        ),
    ),
)
```

This uses all three-opponent subsets of the reference roster, playing ten games
per subset for each compared strategy. Select five players to use four-opponent
subsets. `backgrounds` can instead specify opponent tuples, including duplicate
strategies. Alternatively, use `background_population` and `background_draws`
to select random opponent lineups. Specify exactly one background source.

Controlled and replacement schedules rotate seats across their requested games.
Advanced `reverse_order` and `additional_permutations` settings allocate that
same game budget across different orders. They do not add games. Extra games
against a lineup use new deals and fresh bot instances.

## Python execution and reanalysis

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from sixnimmt.analytics.evaluation import analyse_run
from sixnimmt.analytics.models import AnalysisSpec
from sixnimmt.arena.artifacts import load_run
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.planned import run_plan
from sixnimmt.arena.planning import CandidateConfig, LineupConfig, build_arena_plan

settings = LineupConfig(
    catalogue=(
        CandidateConfig(bot="closest_gap", family="board"),
        CandidateConfig(bot="lowest_card", family="card_order"),
    ),
    player_counts=(3,),
    games=3,
    seed=1234,
    execution=RunConfig(concurrency=2),
    analysis=AnalysisSpec(bootstrap_samples=100, evidence_label="development"),
)
plan = build_arena_plan(settings)
run = run_plan(plan)
report = analyse_run(run)
assert run.artifact_dir is None
print(report.diagnostics.finished_matches)

with TemporaryDirectory() as temporary:
    directory = Path(temporary) / "comparison"
    run = run_plan(plan, output_dir=directory, trace=True)
    report = analyse_run(run)
    saved_report = analyse_run(load_run(directory))
    assert report == saved_report
    print(report.diagnostics.finished_matches)
```

`run_plan(plan)` returns compact outcomes in memory and writes nothing.
`ArenaRun.artifact_dir` is `None` for these runs. Supply `output_dir` to save
evidence; `trace=True` requires it. The example also demonstrates a temporary
saved run. Use a persistent path to retain that evidence. Process execution requires
an importable script with a `__main__` guard. Both backends construct independent
bots for each match and bound the number of games running at once.

`analyse_run` accepts an optional `AnalysisSpec` for reanalysis. This records
cutoffs, confidence level, resampling count and seed, filters, practical-effect
threshold, and evidence labels. Unspecified evidence never implies confirmation;
analysis selected after inspecting results is exploratory.

For example, save a larger process run with an explicit output directory:

```bash
uv run sixnimmt arena --games 10000 --player-count 4 \
  --backend process --concurrency 4 --output-dir runs/comparison
```

Read its saved analysis without recalculating it:

```python
import gzip

from sixnimmt.analytics.models import EvaluationReport

with gzip.open("runs/comparison/analysis.json.gz", "rt", encoding="utf-8") as source:
    report = EvaluationReport.model_validate_json(source.read())
print(report.diagnostics.finished_matches)
```

## Saved evidence and interpretation

When `--output-dir` is supplied, the output directory contains five files:

| File | Contents |
| --- | --- |
| `plan.json` | Frozen catalogue, complete planned schedule, seeds, rules, execution settings, and declared analysis settings |
| `results.jsonl` | Compact factual outcomes, saved as games return |
| `manifest.json` | Execution status, runtime provenance, and artifact references |
| `report.md` | Formatted strategy comparison and readable supporting results |
| `analysis.json.gz` | Full typed analysis, stored as gzip-compressed JSON |

The CLI writes the two reports after execution. Python callers can use
`analyse_run` with the evidence saved by `run_plan`. Optional full traces live
in `traces/` under the same directory. `--json` still prints the full analysis
as pretty-printed JSON to standard output; when saved, the file stays compressed.
Without `--output-dir`, the CLI prints results and writes none of these files.

Compact outcomes retain final scores, winners, completed-hand scores, action and
rejection counts, available timings and resources, and failure context.
Decision counts and total time are retained by default. Individual call timings
are collected only with `--trace`, which also enables median and 95th-percentile
decision timings. Reanalysis of an untraced run preserves aggregate timings and
all competitive results; it reports decision quantiles as unavailable.
Unfinished games have no competitive scores. Planned games remain visible even
if execution stops before they start. A supplied output directory must not already exist.

Win credit splits one unit equally among tied winners. Top-half credit is the
exact chance of qualifying under random tie breaking; the default cutoff is
half the players, rounded up. That means top two with four players, top three
with five or six, and top five with ten. The objectives are reported separately.

Random-lineup estimates use only their finished appearances. Controlled extra
games do not alter that average. Weighted population estimates retain declared
opponent weights: missing required opponents make a population unsupported,
without treating missing results as zero or silently redistributing their weight.
Large populations summarize unobserved combinations and their probability instead
of printing thousands of empty rows.

Uncertainty calculations keep related observations together. Multiple seats
from one game are not independent games, and matched candidate/reference games
share a deal. Completion coverage and missing-outcome bounds accompany estimates.
Only finished matches contribute competitive penalties; a partial hand cannot
be counted as a successful low score.

The Markdown report starts with the main strategy table. Exploratory views of
opponent combinations, copy counts, population sensitivity, and seat order use
collapsed sections with limited previews. All results remain available in
`analysis.json.gz`. Sparse evidence remains explicit, and selected weakest
opponents are labelled exploratory. A default two-percentage-point practical
threshold identifies useful gains or practical equivalence; other comparisons
remain unresolved.

A completed workflow can contain failed bots or inconclusive results. The CLI
returns 0 when the requested workflow completes, 2 for invalid configuration,
and 1 for execution errors, early stopping, or analysis errors. Errors identify
saved evidence when an output directory was supplied; early stopping still
produces a coverage report when possible.

Fixed seeds do not guarantee identical results after changes to code,
dependencies, model providers, hardware, or timeouts. Available provenance is
retained with each run and saved when an output directory is supplied.
