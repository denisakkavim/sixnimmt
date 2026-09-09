# sixnimmt

A deterministic 6 nimmt! rules engine and in-process arena for trusted bots.
Run experiments with scripted strategies or tool-calling LLM players, then replay
the recorded games and compare their results.

The arena supports classic play and communication with table/direct messages,
explicit commitment, and revised selections. Bots receive filtered player views;
the engine validates their actions. Built-in strategies include eight [baseline heuristics](docs/bots.md#built-in-baselines-and-board-and-hand-heuristics),
plus `llm` and `llm_memory`.

## Quick start

Requires Python 3.13 or newer and [uv](https://docs.astral.sh/uv/).
Run these commands from the repository root:

```bash
uv sync --all-groups
uv run sixnimmt arena --players-file examples/arena-players.json --games 10 --seed 1234
```

The [player file](examples/arena-players.json) configures one bot per seat.
Lower scores are better. Output distinguishes finished, abandoned, forfeited,
and failed matches; only finished matches contribute scores, wins, and ties.

Add `--communication` to enable messaging and explicit commitment. Add
`--trace-dir traces/my-run` to save logs and a manifest in a new directory.
Use `uv run sixnimmt --help` to see the `arena`, `replay`, and `summarise` commands.

## Python usage

```python
from sixnimmt.arena.bots import LowestFittingCardBot, RandomBot
from sixnimmt.arena.runner import run_match

result = run_match([RandomBot(11), LowestFittingCardBot()], seed=1234)
print(result.outcome, result.winners)
print([(player.player_id, player.total_score) for player in result.final_state.players])
```

Custom bots implement `act(view, rejection=None)` and return an `Action` or
`ActionBatch`. They are trusted Python code running in the same process.
For LLM experiments, configure the endpoint and model in the player file and
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
- [Writing bots](docs/bots.md)
- [LLM players and private memory](docs/llm-players.md)
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
