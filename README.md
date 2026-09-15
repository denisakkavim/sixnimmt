# sixnimmt

A deterministic 6 nimmt! rules engine and in-process arena for trusted bots.
Run experiments with scripted strategies or tool-calling LLM players, then replay
the recorded games and compare their results.

The arena supports classic play and communication with table/direct messages,
explicit commitment, and revised selections. Bots receive filtered player views;
the engine validates their actions. Built-in strategies include eight [baseline heuristics](docs/bots.md#built-in-baselines-and-board-and-hand-heuristics),
plus `llm` and `llm_memory`. [External harness players](docs/harness-players.md)
also support visible Codex/Claude terminals and supervised headless commands.

Use `--backend process --concurrency 4` to run arena matches across multiple CPU
cores. The default thread backend also supports concurrent matches.

## Quick start

Requires Python 3.13 or newer and [uv](https://docs.astral.sh/uv/).
Run these commands from the repository root:

```bash
uv sync --all-groups
uv run sixnimmt play --games 100 --player-count 4
```

Without fixed seats, the run compares reference strategies across fresh opponent lineups.
Use `--config` with an [arena configuration](examples/arena.json) to choose
strategies and settings. Player counts from 2 to 10 are supported. Runs stay in
memory and print readable results by default. Add `--output-dir runs/first-run`
to save compact results, a readable `report.md`, and the full structured analysis
in `analysis.json.gz`.

Add `--communication` to enable messaging and explicit commitment. Add
`--trace` with `--output-dir` to also save detailed game logs in its `traces/` folder.
Interactive terminals show animations while games run and reports are prepared;
use `--no-animation` to disable them.
To repeat and watch an exact lineup, use the same command:

```bash
uv run sixnimmt play --seat random --seat lowest_fitting_card --games 2 --hands 1 --watch
```

Fixed lineups can also mix native harnesses, headless agents, and built-in bots.
One configuration file controls lineup, execution, sessions, display, and recording.
Use `uv run sixnimmt play --help` for all options.

## Python usage

```python
from sixnimmt.application import run
from sixnimmt.arena.planning import CandidateConfig, RunSettings

result = run(
    RunSettings(
        catalogue=(CandidateConfig(bot="random"), CandidateConfig(bot="lowest_fitting_card")),
        lineup=("random", "lowest_fitting_card"),
        seed=1234,
    )
)
print(result.report.diagnostics.finished_matches)
print(result.run.results[0].scores)
```

Custom bots implement `act(view, rejection=None)` and return an `Action` or
`ActionBatch`. They are trusted Python code running in the same process.
For LLM comparisons, configure the endpoint and model in the configuration file and
set provider timeouts plus an arena `--decision-timeout`.

Seeded scripted strategies are reproducible within the same runtime and
configuration. External model responses are not guaranteed to repeat, but
recorded game events can be replayed without calling the model again.

## Documentation

Start with the [documentation index](docs/README.md) or jump to:

- [Getting started](docs/getting-started.md)
- [Using the Python package](docs/python-api.md)
- [Game rules and information boundaries](docs/game-rules.md)
- [Arena configuration and limits](docs/arena.md)
- [Strategy comparisons and saved reports](docs/comparisons.md)
- [Writing bots](docs/bots.md)
- [LLM players and private memory](docs/llm-players.md)
- [External harness players and watched tables](docs/harness-players.md)
- [Traces, replay, and analytics](docs/traces.md)
- [Architecture and development](docs/development.md)

## Development

```bash
uv run pytest
```

The default suite excludes the long-running volume tests. To run everything:

```bash
uv run pytest -o addopts=''
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks, and pull request guidance.
