# Using sixnimmt from Python

Use `sixnimmt.application.run(settings)` for fixed lineups, sampled experiments,
and external players. One `RunSettings` model controls the schedule, execution,
sessions, display, recording, and analysis. Every variant returns
`RunResult(run, report, artifacts)`: compact factual evidence, its typed analysis,
and published report paths (or `None` for an in-memory run).

For integrations that need separate steps, use `build_arena_plan(settings)`,
`run_plan(plan)`, and `analyse_run(evidence)`. `run_match` is the lower-level
primitive for directly supplied bot instances and a complete game state/event
history. Use the engine directly to control each action.

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

Imports use `sixnimmt`, for example `sixnimmt.application`. The top-level
package does not re-export the APIs below.

## Run and inspect games

```python
from sixnimmt.application import run
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.planning import CandidateConfig, RunSettings
from sixnimmt.engine.rules import MatchProtocol

settings = RunSettings(
    catalogue=(
        CandidateConfig(bot="random", label="Alice"),
        CandidateConfig(bot="lowest_fitting_card", label="Bob"),
    ),
    lineup=("random", "lowest_fitting_card"),
    games=10,
    seed=1234,
    protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
    execution=RunConfig(concurrency=2),
)
result = run(settings)
print(result.report.diagnostics.finished_matches)
for match in result.run.results:
    print(match.match_id, match.outcome, match.scores, match.winners)
assert result.artifacts is None
```

`lineup` names catalogue keys in seat order; each key defaults to its bot name.
It can repeat a key, creating independent copies. Fixed lineups default to one
game; `games` repeats that lineup with fresh players and new deal/bot seeds.
Omit `lineup` to sample opponents from the catalogue. A sampled schedule defaults
to 100 games at four players and requires anonymous player names. The
[comparison guide](comparisons.md#python-execution-and-reanalysis) covers controlled
and replacement schedules and population interpretation.

`ArenaRun` in `arena.artifacts` contains the frozen `plan`, compact `results`,
execution `status`, and runtime `provenance`. `MatchRecord.scores` and
`completed_hand_scores` align with its seats; `winners` contains zero-based seat
indices. Unfinished matches have no competitive scores or winners, although they
can retain partial scores for diagnosis. Check completion coverage alongside
estimates. Plans and evidence are privileged: they include seeds and may include
private options or failure context.

`EvaluationReport.context` is a typed `ReportContext`: for example,
`result.report.context.rules.target_score` accesses the game setting. Both fixed
and sampled runs use the same analysis models and report renderers. Fixed results
describe the opponents and seats that were played; population estimates require
the corresponding experimental evidence.

`run` is synchronous. In an async application, invoke it from an appropriate
worker instead of blocking the event loop. `execution.concurrency` limits
simultaneous matches. CPU-heavy registered strategies can use
`RunConfig(backend="process", concurrency=4)` in an importable script protected
by `if __name__ == "__main__"`. Factories and options models must be picklable;
bot instances need not be. See [process execution](arena.md#timeouts-and-concurrency).

## Configure the game and its limits

`RunSettings` separates `rules`, `protocol`, and `execution`.
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

Pass these objects as `RunSettings(rules=rules, protocol=protocol, execution=config, ...)`. The protocol action budget
is per player; the arena play limit counts attempts across all seats. Choose a
longer deadline for provider calls as needed. Deadlines discard late results but
cannot cancel arbitrary Python code or an outstanding provider request.

Execution normally expresses bot failures through match outcomes. Configuration
validation can raise `ValueError` or Pydantic validation errors. Fatal tracing,
worker, or scheduling errors raise `RunExecutionError` from `arena.execution`,
which carries available partial run evidence. Do not assume every exception becomes a match result.
See [Running games](arena.md) for defaults and outcome semantics.

For model seats, use `CandidateConfig(bot="llm", options={...})` or
`CandidateConfig(bot="llm_memory", options={...})` in the catalogue. Set `model`, `base_url`, and, where
needed, `api_key_env` in options. Provider calls can incur costs; the package does
not calculate monetary totals. [LLM players](llm-players.md) documents the options.

For mixed native, headless, and registered players, supply an explicit `lineup`
in the same `RunSettings`. External names include `codex`, `claude`,
`codex-headless`, `claude-headless`, and `command`. Configure a headless model in
`CandidateConfig(bot="codex-headless", options={"model": "YOUR_CODEX_MODEL"})`.
External seats require the thread backend; attached native sessions require
concurrency one. See [external configuration](harness-players.md#configure-models-and-a-lineup).

Set `session=SessionOptions(...)` for readiness, managed invocation, retention,
and notebook policy. These options are independent of game decision deadlines.
`display=DisplayOptions(watch=True)` selects watch behavior and requires thread
execution with concurrency one. The Python application accepts `observer`,
`on_activity`, `report`, and `confirm_start` callbacks without importing terminal
code. Supply a confirmation callback or `session.auto_start=True` when interactive
startup is requested. The CLI attaches its own live renderer with `--watch`.

## Save, replay, and summarize a trace

This self-contained example saves the same evidence and reports as the CLI.
It uses a temporary directory and removes it afterward; use a persistent new path
to retain the output.

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from sixnimmt.analytics.summary import summarise
from sixnimmt.application import run
from sixnimmt.arena.artifacts import load_run
from sixnimmt.arena.config import RecordingOptions
from sixnimmt.arena.planning import CandidateConfig, RunSettings
from sixnimmt.engine.replay import replay_events
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.persistence.manifest import read_manifest_entry
from sixnimmt.persistence.sink import read_action_log, read_event_log

with TemporaryDirectory() as temporary:
    directory = Path(temporary) / "experiment"
    result = run(
        RunSettings(
            catalogue=(CandidateConfig(bot="random"), CandidateConfig(bot="lowest_fitting_card")),
            lineup=("random", "lowest_fitting_card"),
            seed=1234,
            protocol=MatchProtocol(end_condition="fixed_hands", hands=1),
            recording=RecordingOptions(output_dir=directory, trace=True),
        )
    )
    loaded = load_run(directory)
    match = loaded.results[0]
    assert match.event_trace is not None
    log = directory / match.event_trace
    entry = read_manifest_entry(directory / "traces" / "manifest.json", log.name)
    events = read_event_log(log)
    assert entry.actions is not None
    actions = read_action_log(log.parent / entry.actions)
    replayed = replay_events(events)
    summary = summarise(events, actions, entry)
    assert replayed.status == "finished"
    assert result.artifacts is not None
    print(result.artifacts.report)
    print(summary.model_dump_json(indent=2))
```

Replay folds events without calling bots or resuming the game. Use recorded
filenames and retain manifest outcome context when summarizing. Replay reconstructs
the undealt remainder's contents without promising their original order.

`load_run` reads `plan.json`, `results.jsonl`, and `manifest.json`. It validates
result identities against planned jobs and reconciles committed outcomes newer
than the last status snapshot. `analyse_run` applies the same validation to
in-memory evidence. `run_plan(plan, output_dir=..., trace=True)` saves evidence
without publishing reports; the application publishes `report.md` and
`analysis.json.gz` and returns their actual paths. See [Traces and replay](traces.md).

A frozen plan stores an external command's construction fingerprint rather than
its private argument list. `application.run(settings)` supplies original options
privately to execution. To reuse such a plan directly, pass
`run_plan(plan, players=inputs_by_config_id, ...)`, where each value is a
`PlayerConfig` with the original bot and options. The executor validates their
fingerprints and implementation identity before preparing seats. Analysis and
replay do not need these construction inputs or a running model.

## Run supplied bot instances

```python
from sixnimmt.arena.bots.heuristics import LowestFittingCardBot, RandomBot
from sixnimmt.arena.results import MatchOutcome
from sixnimmt.arena.match import run_match
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
Use `sixnimmt.arena.results.public_summary(result)` for public output: it includes
scores, winners and safe termination codes, excluding private exception details.

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

`run_match` and `application.run` also accept
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
enabled tracing saves managed diagnostics even when the CLI display is hidden. See [the live table display](harness-players.md#watch-the-live-game)
and [model diagnostics](traces.md#model-diagnostics).

`run(settings, on_progress=callback)` calls `callback(completed)` after each
collected match result, including non-finished outcomes. Counts start at 1 and
increase once per result. The callback runs in the calling thread for both
backends and receives only the count, without game state. Keep it quick;
callback exceptions propagate to the caller. Early stops may leave the count
below the requested number of games.

## API map

| Import | Purpose |
| --- | --- |
| `sixnimmt.application.run` | Shared plan, execution, analysis, and publication workflow |
| `sixnimmt.arena.planning.RunSettings`, `CandidateConfig` | Common configuration and catalogue entries |
| `sixnimmt.arena.planning.build_arena_plan` | Freeze explicit assignments, identities, and seeds |
| `sixnimmt.arena.execution.run_plan` | Execute the same plan independently of analysis |
| `sixnimmt.arena.match.run_match` | Run directly supplied bot instances |
| `sixnimmt.arena.artifacts` | Compact results, validated saved evidence, and report publication |
| `sixnimmt.arena.players.PlayerConfig` | Registered strategy and seat settings |
| `sixnimmt.arena.config` | Execution, session, display, and recording settings |
| `sixnimmt.arena.results` | Individual match outcomes and public summaries |
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


## Construction and configuration scopes

`RunSettings` is the common configuration boundary for fixed and sampled jobs.
Counts require integers, excluding booleans and quoted numbers; probabilities
and durations require finite numeric values. Design and policy flags require
booleans. Catalogue options require finite JSON values and are then validated by
the selected strategy or external-seat options model. Unsupported capability
combinations fail before execution.

`RunConfig` describes operational limits. Internally, resolving it produces
separate match limits, run limits, and recording policy. `SessionOptions` owns
external readiness, invocation, retention, and notebooks; `DisplayOptions` owns
presentation; `RecordingOptions` owns the common output directory and trace flag.
Session, display, and recording settings do not alter plan identities or seeds.

`ResolvedStrategy` contains validated strategy options, metadata, and a seed-only
factory. `ResolvedPlayer` adds seat identity. `BotSpec.typed` checks an options
schema and its constructor together; see [Writing bots](bots.md). External seat
descriptors and construction live with session supervision in `arena.sessions`.
They validate and describe capabilities without starting processes during planning.
