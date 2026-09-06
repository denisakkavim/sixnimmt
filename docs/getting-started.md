# Getting started

Install Python 3.13 or newer and `uv`, then install the project and development tools:

```bash
uv sync --all-groups
uv run sixnimmt --help
uv run sixnimmt arena --players-file examples/arena-players.json --games 10 --seed 1234
```

The bundled [player file](../examples/arena-players.json) supplies the lineup.
You can create your own JSON file with two to ten entries:

```json
[
  {"bot": "random", "display_name": "Alice"},
  {"bot": "greedy", "display_name": "Bob"}
]
```

Each array position is a seat. The arena assigns IDs `player_1`, `player_2`, and
so on, preserving the order for every match. Lower scores are better. The output
reports finished and unfinished outcomes separately; only finished matches count
toward scores, wins, and ties. A win means a sole winner; a tie means sharing the
lowest score.

## Save an experiment

Choose a trace directory that does not exist yet:

```bash
uv run sixnimmt arena --players-file examples/arena-players.json --games 2 --seed 1234 --trace-dir traces/first-run
```

Open `traces/first-run/manifest.json` and find a match's `log` filename. Substitute
that filename for `MATCH_LOG.jsonl` below:

```bash
uv run sixnimmt replay traces/first-run/MATCH_LOG.jsonl
uv run sixnimmt summarise traces/first-run/MATCH_LOG.jsonl
```

Replay reconstructs the final game state without calling the bots again. Summary
adds action, messaging, latency, and outcome information from the neighboring
action log and manifest. See [Traces and replay](traces.md) for file details.

## Run a match from Python

For installation in another project and more integration examples, see
[Using sixnimmt from Python](python-api.md).

```python
from sixnimmt.arena.bots import GreedyBot, RandomBot
from sixnimmt.arena.runner import run_match
from sixnimmt.engine.rules import MatchProtocol

result = run_match(
    [RandomBot(11), GreedyBot()],
    seed=1234,
    protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
)
print(result.outcome, result.winners)
print([(player.player_id, player.total_score) for player in result.final_state.players])
```

`run_match` receives bot instances; construct new instances for independent
matches. For a repeated experiment, `run_arena` constructs fresh instances for
each match from [player configurations](arena.md).

## Enable communication

Add `--communication` to an arena command. This enables messages and explicit
commitment; it does not force bots to talk. The bundled random and greedy bots
select and commit without sending messages. Custom bots and LLM players can use
the extra actions. See [Game rules and information](game-rules.md).
