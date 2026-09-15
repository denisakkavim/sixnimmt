# sixnimmt

A deterministic 6 nimmt! rules engine and in-process arena for trusted bots.
Run experiments with scripted strategies or tool-calling LLM players, then replay
the recorded games and compare their results.

The arena supports classic play and communication with table/direct messages,
explicit commitment, and revised selections. Bots receive filtered player views;
the engine validates their actions. Built-in strategies include eight [baseline heuristics](docs/bots.md#built-in-baselines-and-board-and-hand-heuristics),
plus `llm` and `llm_memory`. [External harness tables](docs/harness-players.md)
also support visible Codex/Claude terminals and supervised headless commands.

Use `--backend process --concurrency 4` to run arena matches across multiple CPU
cores. The default thread backend also supports concurrent matches.

## Quick start

Requires Python 3.13 or newer and [uv](https://docs.astral.sh/uv/).
Run these commands from the repository root:

```bash
uv sync --all-groups
uv run sixnimmt arena --games 100 --player-count 4
```

The arena compares the reference strategies across fresh opponent lineups.
Use `--config` with an [arena configuration](examples/arena.json) to choose
strategies and settings. Player counts from 2 to 10 are supported. Runs stay in
memory and print readable results by default. Add `--output-dir runs/first-run`
to save compact results, a readable `report.md`, and the full structured analysis
in `analysis.json.gz`.

Add `--communication` to enable messaging and explicit commitment. Add
`--trace` with `--output-dir` to also save detailed game logs in its `traces/` folder.
Interactive terminals show animations while games run and reports are prepared;
use `--no-animation` to disable them.
Use `uv run sixnimmt --help` for the available commands, including `table` for
an explicit lineup of native harnesses, headless agents, and built-in bots.

## Python usage

```python
from sixnimmt.arena.bots.heuristics import LowestFittingCardBot, RandomBot
from sixnimmt.arena.runner import run_match

result = run_match([RandomBot(11), LowestFittingCardBot()], seed=1234)
print(result.outcome, result.winners)
print([(player.player_id, player.total_score) for player in result.final_state.players])
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
