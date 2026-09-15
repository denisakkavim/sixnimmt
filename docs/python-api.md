# Using sixnimmt from Python

Use `run_match` when you have bot instances and want one game's state and events.
Use `run_arena` for repeated experiments with fresh bots and aggregate results.
Use `build_arena_plan`, `run_plan`, and `analyse_run` for changing lineups and
reusable comparison evidence; see the [comparison API example](comparisons.md#python-execution-and-reanalysis).
Use the engine directly when your application needs to control each action.

`run_plan(plan)` keeps its outcomes in memory and writes nothing; its returned
`ArenaRun.artifact_dir` is `None`. Supplying `output_dir` saves the evidence, and
`trace=True` requires that directory. `load_run` reconstructs saved evidence from
`plan.json`, `results.jsonl`, and `manifest.json`. With `--output-dir`, the CLI also
writes a readable `report.md` and the full typed analysis in `analysis.json.gz`.
The comparison guide shows how to recalculate results with `analyse_run` or read
the compressed analysis directly.

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
from sixnimmt.arena.bots.heuristics import LowestFittingCardBot, RandomBot
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
`lifecycle_errors` retains privileged cleanup/notification diagnostics separately
from an established match outcome; trace manifests also include these in `stats_errors`.

For an operator-controlled local match, pass `stop_event=threading.Event()` to
`run_match` and set the event from another thread to stop play. This interrupts
arena waiting, invokes any bot cancellation hook, and records `abandoned` with
reason `operator_stop`. A finished match stays finished if completion won the
race. Optional [bot lifecycle hooks](bots.md#optional-lifecycle-hooks) let external
adapters release resources; arbitrary trusted Python code cannot be forcibly
terminated by a thread event.

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
matches for later analysis, or `run_plan` to retain compact comparison outcomes
without full traces. Only finished matches contribute scores, wins, and
ties; always inspect failure counts alongside averages.

These runners are synchronous and block until their work completes. `concurrency`
controls match workers within the run. An async application should run them in
an appropriate worker rather than directly on its event loop.

For CPU-heavy tournaments, use `RunConfig(backend="process", concurrency=4)`.
The default backend is `thread`. Process mode constructs bots inside spawned
workers and requires an importable Python entry module with a
`if __name__ == "__main__"` guard. It supports importable, picklable custom
factories but rejects live observers and local/lambda factories. See
[process execution](arena.md#timeouts-and-concurrency) for execution requirements,
timeout semantics, and trace handling. These worker settings affect `run_arena`;
`run_match` always runs locally.

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

For mixed native, headless, and registered-bot tables, `TableConfig` in
`sixnimmt.arena.table` validates the [table configuration file](harness-players.md#configure-models-and-a-lineup).
Its `players()` method resolves the ordered lineup into `PlayerConfig` objects;
passing seat specifications to that method replaces the saved lineup. The
module's `create_seats` and `run_table` accept these objects as well as CLI-style
seat strings. Headless models use `PlayerConfig(bot="codex-headless",
options={"model": "YOUR_CODEX_MODEL"})` or the corresponding `claude-headless`
entry. These external bot names are supported by the table controller.

## Save, replay, and summarize a trace

This self-contained example uses a temporary directory and removes it afterward.
For persistent output, replace that directory with a path in your application.
The runner's `trace_dir` itself must not already exist.

```python
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from sixnimmt.analytics.summary import summarise
from sixnimmt.arena.bots.heuristics import LowestFittingCardBot, RandomBot
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

`run_match` and `sixnimmt.arena.table.run_table` also accept
`on_activity(record)`. The callback receives dictionaries for decision start and
completion, match completion, model output, managed invocation/repair activity,
and available simulation candidate values. It can be called from decision or
output threads, so it must be fast and thread-safe. Queue work for a display
thread if rendering or writing it could block.

Decision start and completion records include a per-seat `decision_number`,
`view_id`, `hand_number`, `play_number`, `phase`, and `cards_remaining`. These
describe the offered decision even when publishing it advances the game. A
completion record's `actions` contains only accepted actions; rejected or failed
attempts appear under `attempted_actions`. Action identifiers and notebook
contents are excluded from these summaries. Managed `decision_id` values remain
separate identifiers; use the associated `view_id` and player to connect them
to the arena's active decision.

Simulation activity includes `chosen_card`, `candidate_values`, `objective`,
`sample_count`, and `horizon_plays` when available. The chosen card comes from
the accepted action; the horizon is capped at the cards remaining in that hand.
These values describe the completed card evaluation and are not repeated for a
later commit or row choice.

Activity records are privileged diagnostics and can reveal cards or plans.
They do not change game state and are not input to event replay. Use public
event filtering for a spectator board, and label any activity pane as operator
information. Attaching a callback is independent of saving a model trace;
`run_table` saves managed diagnostics in its output directory even when the CLI
display is hidden. See [the live table display](harness-players.md#watch-the-live-game)
and [model diagnostics](traces.md#model-diagnostics).

`run_arena(..., on_progress=callback)` calls `callback(completed)` after each
collected match result, including non-finished outcomes. Counts start at 1 and
increase once per result. The callback runs in the calling thread for both
backends and receives only the count, without game state. Keep it quick;
callback exceptions propagate to the caller. Early stops may leave the count
below the requested number of games.

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

Event payloads are Pydantic-validated dictionaries with event-specific `TypedDict`
types. For example, constructing a `CardPlacedEvent` requires `card`, `row`, and
`row_cards` in `data`; an empty payload raises a validation error immediately.
Check an event's `type` before accessing specific payload fields. Serialized event
structure is unchanged; see [event contracts](traces.md#event-payload-contracts).

## Configuration validation

New `GameRules` and `InformationPolicy` input rejects unknown keys. The published
hand, deck, and row sizes are literal schema values, and a new target score must
be positive. `max_message_length` must be nonnegative; zero allows only empty
messages when communication is enabled. An irrelevant `hands` setting under
score-based termination remains accepted for compatibility.

Historical configuration is decoded through `rules_from_recording()` and
`protocol_from_recording()`, which preserve older numeric values and tolerate
unknown keys when folding logs. These readers are not experiment-input validators.
Engine state transitions may use unchecked `model_copy(update=...)` after
establishing invariants; changing external configuration requires validation.

## Action metadata

`ActionEnvelope.action_id` and `expected_view_version` are retained compatibility
metadata. The in-process arena generates its own action-record IDs and neither
deduplicates by the supplied ID nor enforces an optimistic-concurrency guard.
`from_view` is retained caller metadata with no freshness check. The arena
records its own offered view in action audit records.
