# Getting started

Install Python 3.13 or newer and `uv`, then install the project and development tools:

```bash
uv sync --all-groups
uv run sixnimmt --help
uv run sixnimmt play --games 10 --seed 1234
```

This plays ten four-player games using the reference strategy catalogue and
prints the comparison results. Everything stays in memory; no files or run
directory are created. Add `--output-dir runs/first-comparison` to save evidence
and a readable `report.md`. Choose a new directory for each saved run.

Interactive terminals show animations while games run and the analysis is
prepared. Use `--no-animation` to disable both. Piped output and `--json`
suppress them automatically; `--json` alone saves no files.

## Choose strategies and table sizes

The bundled [configuration](../examples/arena.json) supplies two strategies:

```json
{
  "catalogue": [
    {"bot": "random", "label": "Random"},
    {"bot": "lowest_fitting_card", "label": "Lowest fitting card"}
  ]
}
```

```bash
uv run sixnimmt play --config examples/arena.json \
  --games 10 --player-count 3 --player-count 6 --output-dir runs/two-sizes
```

This requests ten games at each table size, twenty games total. Any player count
from two through ten is supported. Each game draws its lineup from the catalogue;
the same configuration may occupy several seats. Catalogue entries are strategy
choices, not a fixed seating order. Bots receive anonymous names.

Only finished games contribute competitive outcomes. Reports show unfinished
games separately, and split win credit equally among tied lowest-score players.
See [Running games](arena.md) for options and
[Comparing strategies](comparisons.md) for interpreting the report.

## Save detailed traces

Supply `--output-dir` to save compact results. Add `--trace` for full game logs;
tracing requires an output directory:

```bash
uv run sixnimmt play --config examples/arena.json \
  --games 2 --seed 1234 --output-dir runs/first-traced-run --trace
```

Open `runs/first-traced-run/traces/manifest.json` and find a match's `log`
filename. Substitute that filename for `MATCH_LOG.jsonl` below:

```bash
uv run sixnimmt replay runs/first-traced-run/traces/MATCH_LOG.jsonl
uv run sixnimmt summarise runs/first-traced-run/traces/MATCH_LOG.jsonl
```

Replay reconstructs the final game state without calling the bots again. Summary
adds action, messaging, latency, and outcome information from the neighboring
action log and trace manifest. See [Traces and replay](traces.md) for file details.

## Fix the lineup and watch

Use the same command with ordered seats; `--games` repeats that lineup:

```bash
uv run sixnimmt play --seat random --seat lowest_fitting_card --games 2 --hands 1 --watch
```

A fixed lineup defaults to one game. `--watch` selects live presentation and does
not change execution or recording. To save this run, add `--output-dir` and,
for detailed logs, `--trace`, just as for a sampled schedule.

## Run games from Python

The same `RunSettings` model backs the CLI and Python application API:

```python
from sixnimmt.application import run
from sixnimmt.arena.planning import CandidateConfig, RunSettings
from sixnimmt.engine.rules import MatchProtocol

result = run(
    RunSettings(
        catalogue=(CandidateConfig(bot="random"), CandidateConfig(bot="lowest_fitting_card")),
        lineup=("random", "lowest_fitting_card"),
        games=2,
        seed=1234,
        protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
    )
)
print(result.report.diagnostics.finished_matches)
print(result.run.results[0].scores)
```

Omit `lineup` to sample games from the catalogue. Each job constructs fresh players
and retains its assigned match and private bot seeds. See the
[Python guide](python-api.md) for installation, saved evidence, and low-level APIs.

## Enable communication

Add `--communication` to a `play` command. This enables messages and explicit
commitment; it does not force bots to talk. Baseline bots select and commit
without sending messages. Custom bots and LLM players can use the extra actions.
See [Game rules and information](game-rules.md).
