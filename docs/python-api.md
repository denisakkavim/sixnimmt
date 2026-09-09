# Using sixnimmt from Python

Use `run_match` when you have bot instances and want one game's state and events.
Use `run_arena` for repeated experiments with fresh bots and aggregate results.
Use the engine directly when your application needs to control each action.

## Install in another project

The package requires Python 3.13 or newer. From your application's directory,
add your local checkout as an editable dependency, replacing the example path:

```bash
uv add --editable /absolute/path/to/checkout
```

Alternatively, build a wheel from the sixnimmt checkout:

```bash
uv build --wheel
```

Then, from your application, install that wheel:

```bash
uv add /absolute/path/to/checkout/dist/sixnimmt-0.0.1-py3-none-any.whl
```

Use the filename produced by the build if the version changes. These instructions
use your checkout or build artifact and do not require a published PyPI release.
Inside this repository, `uv sync --all-groups` already installs the package.
Run application scripts with `uv run python your_script.py`.

Imports use `sixnimmt`, for example `sixnimmt.arena.runner`. The top-level
package does not re-export the APIs below.

## Run and inspect one match

```python
from sixnimmt.arena.bots import LowestFittingCardBot, RandomBot
from sixnimmt.arena.results import MatchOutcome
from sixnimmt.arena.runner import run_match
from sixnimmt.engine.rules import MatchProtocol

result = run_match(
    bots=[RandomBot(11), LowestFittingCardBot()],
    seed=1234,
    match_id="example",
    protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
)

if result.outcome == MatchOutcome.FINISHED:
    print("Winners:", result.winners)
    for player in result.final_state.players:
        print(player.player_id, player.total_score)
else:
    print(result.outcome, result.ended_by, result.reason)

print("Attempts:", result.actions)
print("Recorded events:", len(result.events))
```

The seed supplied to `run_match` controls the deal. A directly constructed random
bot has its own seed; construct fresh bot instances for independent matches.
Without an explicit protocol, matches play until a completed hand brings a
player to the default target of 66 points. Fixed-hand matches are convenient for
short experiments.

`MatchResult` is a frozen dataclass containing `outcome`, `final_state`, `events`,
`winners`, accepted/rejected action counts, and optional failure context. Lower
scores win. `events` and `final_state` contain privileged information, including
other players' hands and seeds; do not pass them to a bot as its observation.

To name seats, supply `seats=[PlayerSeat(player_id="alice", display_name="Alice"),
...]`, importing `PlayerSeat` from `sixnimmt.engine.state`. Seat order must match
bot order and both sequences must have the same length. Default IDs are
`player_1`, `player_2`, and so on.

## Run repeated experiments

```python
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.arena.runner import RunConfig, run_arena
from sixnimmt.engine.rules import MatchProtocol

arena = run_arena(
    players=[
        PlayerConfig(bot="random", display_name="Alice"),
        PlayerConfig(bot="lowest_fitting_card", display_name="Bob"),
    ],
    games=10,
    seed=1234,
    protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
    config=RunConfig(concurrency=2),
)

print(arena.games_requested, arena.games_started, arena.games_completed)
print(arena.finished, arena.abandoned, arena.forfeited, arena.failed)
for seat in arena.players:
    average = seat.total_score / arena.finished if arena.finished > 0 else None
    print(seat.display_name, seat.wins, seat.ties, average)
```

`run_arena` takes `PlayerConfig` objects, not bot instances or strings. It
constructs a fresh registered bot for each seat and match, deriving separate
match and bot seeds from the root seed. Player options are validated before the
run. See [Writing bots](bots.md#register-a-configurable-strategy) to register your
own factory in the same process.

`ArenaResult` contains aggregate counters and one `SeatResult` per seat. It does
not return a list of all individual match results. Use tracing to retain those
matches for later analysis. Only finished matches contribute scores, wins, and
ties; always inspect failure counts alongside averages.

These runners are synchronous and block until their work completes. `concurrency`
controls match workers within the run. An async application should run them in
an appropriate worker rather than directly on its event loop.

## Configure the game and its limits

Both runners accept `rules`, `protocol`, and `config` as keyword arguments.
This example enables communication but hides the existence of direct messages
from uninvolved players:

```python
from sixnimmt.arena.config import RunConfig
from sixnimmt.engine.rules import GameRules, InformationPolicy, MatchProtocol

rules = GameRules(target_score=66)
protocol = MatchProtocol(
    end_condition="fixed_hands",
    hands=2,
    communication_enabled=True,
    information_policy=InformationPolicy(private_message_existence="hidden"),
    max_actions_per_play=10,
)
config = RunConfig(
    scheduler="round_robin",
    match_action_limit=10_000,
    play_action_limit=200,
    decision_rejection_limit=8,
    decision_timeout_seconds=5.0,
)
```

Pass these three objects to `run_match` or `run_arena`. The protocol action budget
is per player; the arena play limit counts attempts across all seats. Choose a
longer deadline for provider calls as needed. Deadlines discard late results but
cannot cancel arbitrary Python code or an outstanding provider request.

The runners normally express bot failures through match outcomes. Configuration
validation can raise `ValueError` or Pydantic validation errors, and `run_arena`
can raise `ArenaError` for failures such as initial lineup construction or fatal
tracing/scheduling errors. Do not assume every exception becomes a match result.
See [Running arenas](arena.md) for defaults and outcome semantics.

For model seats, use `PlayerConfig(bot="llm", options={...})` or
`PlayerConfig(bot="llm_memory", options={...})`. Set `model`, `base_url`, and, where
needed, `api_key_env` in options. Provider calls can incur costs; the package does
not calculate monetary totals. [LLM players](llm-players.md) documents the options.

## Save, replay, and summarize a trace

This self-contained example uses a temporary directory and removes it afterward.
For persistent output, replace that directory with a path in your application.
The runner's `trace_dir` itself must not already exist.

```python
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from sixnimmt.analytics.summary import summarise
from sixnimmt.arena.bots import LowestFittingCardBot, RandomBot
from sixnimmt.arena.runner import RunConfig, run_match
from sixnimmt.engine.replay import replay_events
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.persistence.manifest import ManifestMatch
from sixnimmt.persistence.sink import read_action_log, read_event_log

with TemporaryDirectory() as temporary:
    trace_dir = Path(temporary) / "experiment"
    live = run_match(
        [RandomBot(11), LowestFittingCardBot()],
        seed=1234,
        protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
        config=RunConfig(trace_dir=trace_dir),
    )
    manifest = json.loads((trace_dir / "manifest.json").read_text())
    entry = ManifestMatch.model_validate(manifest["matches"][0])
    events = read_event_log(trace_dir / entry.log)
    actions = read_action_log(trace_dir / entry.actions)
    replayed = replay_events(events)
    summary = summarise(events, actions, entry)

    assert replayed.winners == live.winners
    print(summary.model_dump_json(indent=2))
```

Replay folds recorded events; it does not run the bots again or resume gameplay.
Use the manifest's filenames and retain its outcome context when summarizing.
When comparing complete states, normalize the undealt remainder's order because
replay reconstructs its contents without promising the original ordering.

The runner owns and closes sinks it creates. If you pass your own `sink` to
`run_match`, you own its lifetime and must close it. An external sink replaces
the runner's automatic trace handling. See [Traces and replay](traces.md).

## Drive the engine one action at a time

Use this level when your application supplies its own interaction loop. Keep
state and event history in the controller, and give each player a filtered view.

```python
from sixnimmt.engine.actions import SelectCardAction
from sixnimmt.engine.audience import Viewer
from sixnimmt.engine.errors import EngineRejection
from sixnimmt.engine.fold import build_view
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.transition import transition
from sixnimmt.engine.views import ViewRole

rules = GameRules()
protocol = MatchProtocol()
state, events = create_match("manual", ["alice", "bob"], 1234, rules=rules, protocol=protocol)
viewer = Viewer(ViewRole.PLAYER, "alice")
view = build_view(events, viewer)
action = SelectCardAction(card=view.you.hand[0])

try:
    next_state, batch = transition(state, "alice", action, protocol, rules)
except EngineRejection as error:
    print(error.code.value, str(error))
else:
    state = next_state
    events = [*events, *batch]
    view = build_view(events, viewer)
    print(view.you.committed)
```

`transition` returns a new state and an event batch; a rejected action leaves its
input state unchanged. The example selects one card, not an entire match.
Your controller must offer the remaining players actions and answer pending row
choices. The arena already implements that loop if you do not need custom control.

`build_view(events, viewer)` folds a complete history. For ongoing applications,
`ViewFolder(viewer)` can consume each new batch with `apply(batch)` and expose the
current observation with `view()`. Apply each batch once and in order.

Engine transitions do not provide the arena's attempt limits, bot deadlines,
persistence, or experiment outcome tracking. A controller that stores logs must
also assign canonical event/action sequence numbers using `assign_sequence` from
`sixnimmt.engine.events`; see the arena runner as the integration reference.

An optional runner `observer(state, events)` callback receives privileged state
and each appended batch, including setup. Use it for instrumentation, not bot
observations. It must be thread-safe when matches run concurrently.

## API map

| Import | Purpose |
| --- | --- |
| `sixnimmt.arena.runner.run_match`, `run_arena` | Synchronous runners |
| `sixnimmt.arena.players.PlayerConfig` | Registered strategy and seat settings |
| `sixnimmt.arena.config.RunConfig` | Scheduling, tracing, and operational limits |
| `sixnimmt.arena.results` | Match and aggregate result dataclasses |
| `sixnimmt.arena.bots` | Built-in bots, registry, contracts, and action batches |
| `sixnimmt.engine.rules` | Game rules and match protocol |
| `sixnimmt.engine.actions` | Typed player actions |
| `sixnimmt.engine.setup`, `transition` | Direct game control |
| `sixnimmt.engine.audience`, `fold` | Filtered observations |
| `sixnimmt.persistence.sink` | Log readers and sinks |
| `sixnimmt.engine.replay.replay_events` | Rebuild recorded state |
| `sixnimmt.analytics.summary.summarise` | Derive per-match metrics |
