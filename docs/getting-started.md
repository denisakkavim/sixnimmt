# Getting started

Install Python 3.13 or newer and `uv`, then install the project and development tools:

```bash
uv sync --all-groups
uv run sixnimmt --help
uv run sixnimmt arena --games 10 --seed 1234
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
uv run sixnimmt arena --config examples/arena.json \
  --games 10 --player-count 3 --player-count 6 --output-dir runs/two-sizes
```

This requests ten games at each table size, twenty games total. Any player count
from two through ten is supported. Each game draws its lineup from the catalogue;
the same configuration may occupy several seats. Catalogue entries are strategy
choices, not a fixed seating order. Bots receive anonymous names.

Only finished games contribute competitive outcomes. Reports show unfinished
games separately, and split win credit equally among tied lowest-score players.
See [Running arenas](arena.md) for options and
[Comparing strategies](comparisons.md) for interpreting the report.

## Save detailed traces

Supply `--output-dir` to save compact results. Add `--trace` for full game logs;
tracing requires an output directory:

```bash
uv run sixnimmt arena --config examples/arena.json \
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

## Run a match from Python

For installation in another project and comparison API examples, see
[Using sixnimmt from Python](python-api.md).

```python
from sixnimmt.arena.bots.heuristics import LowestFittingCardBot, RandomBot
from sixnimmt.arena.runner import run_match
from sixnimmt.engine.rules import MatchProtocol

result = run_match(
    [RandomBot(11), LowestFittingCardBot()],
    seed=1234,
    protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
)
print(result.outcome, result.winners)
print([(player.player_id, player.total_score) for player in result.final_state.players])
```

`run_match` receives bot instances; construct new instances for independent
games. `run_plan` constructs fresh instances for comparison jobs, while
`run_arena` remains available for fixed ordered lineups in Python.

## Enable communication

Add `--communication` to an arena command. This enables messages and explicit
commitment; it does not force bots to talk. Baseline bots select and commit
without sending messages. Custom bots and LLM players can use the extra actions.
See [Game rules and information](game-rules.md).
