# Architecture and development

## Source map

Start at `application.run(RunSettings(...))`. It builds a plan, calls the shared
executor, analyses returned evidence, and publishes reports when requested.
Fixed seats, sampled lineups, native sessions, and watched games use this same
path. The CLI translates arguments and supplies terminal callbacks.

| Package/module | Responsibility |
| --- | --- |
| `application.py` | The common plan, execute, analyse, and publish workflow |
| `cli.py`, `terminal/` | Configuration overrides, exit codes, live/progress presentation, and report rendering |
| `arena/planning.py` | `RunSettings`, populations, concrete assignments, dependency blocks, and seeds |
| `arena/catalogue.py` | Validated catalogue entries, frozen implementation identity, and construction fingerprints |
| `arena/config.py` | Match/run limits, external-session policy, display options, and recording options |
| `arena/execution.py` | Concrete job execution, thread/process pools, bounded submission, outcome collection, and run errors |
| `arena/sessions.py` | External-seat descriptors, mixed seat preparation, readiness, connection retention, and resource cleanup |
| `arena/match.py` | Synchronous match loop, scheduling, atomic proposal preflight/publication, and bot lifecycle |
| `arena/decisions.py` | Decision deadlines, abandoned-call accounting, and per-call measurements |
| `arena/players.py`, `bots/base.py`, `bots/registry.py` | Resolved strategy construction, seat identity, and typed factories |
| `arena/results.py`, `tracing.py` | Individual match outcomes, public summaries, and standalone trace/diagnostic projection |
| `arena/artifacts.py` | Compact evidence models, shared identity validation, provenance, saved-run loading, and report publication |
| `arena/bots/` | Strategies, adapters, normalized diagnostics, and shared agent contracts |
| `arena/bots/external_harnesses/` | Seat protocol, MCP transport, connection bundles, provider streams, and managed processes |
| `engine/cards.py`, `rules.py`, `state.py`, `actions.py`, `events.py` | Game configuration, deterministic dealing, state, and typed actions/events |
| `engine/setup.py`, `transition.py`, `resolution.py`, `lifecycle.py` | Match setup and legal game transitions |
| `engine/audience.py`, `fold.py`, `views.py` | Audience filtering and incremental player observations |
| `engine/replay.py` | Reconstruct state from recorded events |
| `analytics/inputs.py`, `evaluation.py`, `populations.py`, `comparisons.py` | Validated observations and statistical estimates |
| `analytics/projection.py`, `markdown.py`, `terminal.py` | Shared report identities/labels/grouping, then format-specific rendering |
| `persistence/atomic.py`, `sink.py`, `arena.py`, `manifest.py` | Atomic files, recoverable JSONL, durable evidence bytes, and trace-index models |
| `common/` | Small shared value contracts and validation |

The engine owns rules and hidden state. It does not call bots, write files, or
invoke providers. The arena owns execution, scheduling, operational failure
policy, and session resources. Analytics consumes validated evidence. Persistence
owns byte I/O and recovery; typed run interpretation stays in `arena.artifacts`.
Renderers consume report projections and normalized activity, not provider responses.

`RunSettings.lineup` selects ordered fixed seats. Without it, the planner builds
sampled, controlled, or replacement schedules. Both produce the same explicit
jobs and `ArenaRun` result, using `arena-plan-v1` seeds. Session, display, and
recording settings remain outside the frozen experimental design. External
construction is validated without creating resources during planning; private
command arguments are fingerprinted and supplied privately to execution.

The executor selects a thread pool or spawned process pool through `RunConfig`.
Both bound submitted work and construct fresh players for each job. Workers write
per-match traces; the parent collects compact outcomes and updates shared evidence
and trace manifests. Live callbacks and external sessions require threads;
watching and attached native sessions also require concurrency one. Deadline
accounting uses run-wide counters, including process-shared counters where needed.

`application.run` and `run_match` accept fast, thread-safe `on_activity` callbacks
for privileged diagnostics. The live renderer folds public events into a board
and queues output on its own thread. Its separate operator pane can show private
cards or plans; those records never participate in game transitions or replay.
Hiding commentary or board output does not disable enabled tracing.

Commentary is grouped by per-seat decision numbers and managed decision,
invocation, and item identifiers. Only settled actions mark acceptance. Provider
stream parsers extract displayable text and tool activity; only the designated
final output is submitted. Managed proposals use the shared action schema, while
the broker parser and match preflight determine acceptance. Private model logs
retain bounded diagnostic output when tracing is enabled.

Bot implementations live directly in `arena/bots/`, with external support in
`external_harnesses/` and probabilistic strategy support in `uncertainty/`.
`ResolvedStrategy` owns reusable options, provenance, and a seed factory;
`ResolvedPlayer` adds seat identity. Shared instructions and action schemas belong
in `bots/agent_contract.py`. Optional `CandidateEvaluation` diagnostics describe
one completed evaluation independently of cumulative `stats()`.

## Engine API

`create_match` creates and starts a match, returning state and initial events.
For explicit setup sequencing, use `open_match` followed by `start_match`.
`transition` takes a state, actor, action, protocol, and rules, then returns a
new state and event batch. Illegal actions raise `EngineRejection` without
mutating the input state.

```python
from sixnimmt.engine.actions import SelectCardAction
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.transition import transition

rules = GameRules()
protocol = MatchProtocol()
state, events = create_match("example", ["alice", "bob"], 1234, rules=rules, protocol=protocol)
state, batch = transition(state, "alice", SelectCardAction(card=state.players[0].hand[0]), protocol, rules)
events = [*events, *batch]
```

This example is privileged engine integration code. Bot implementations should
use `MatchView` instead of accessing `MatchState`.

Phases progress from setup to selection, then resolution. Resolution can pause
at `awaiting_row_choice`; the selected player supplies a row and resolution
resumes. Completing ten plays banks scores, then either starts another hand or
finishes the match. Abandonment is recorded as lifecycle information without
pretending the game reached normal termination.

Player observations are folded from permitted events. `ViewFolder` maintains
an incremental projection; `build_view` folds a complete history. Keep information
policy enforcement there so every bot type receives the same observation model.

## Validation workflow

[CONTRIBUTING.md](../CONTRIBUTING.md) owns the environment setup, default test,
lint, format, type, and negative static-check commands. Use focused tests while
iterating and run the default suite before handoff. Report default-suite and
full-suite results separately.

The default pytest configuration excludes `arena_slow`. Gameplay and scheduling
changes also require the relevant volume tests. They cover large fixed and sampled
schedules, classic and communication protocols, thread/process equivalence,
intermediate card conservation, scoring, private observations, and replay.
Use `uv run pytest -o addopts=''` for the full suite.

Tests are grouped by observable behavior. LLM tests use controlled HTTP transports
with the real SDK; external harness tests use controlled local processes and MCP
connections. Neither substitutes for an explicitly requested live provider test.

## Making changes

Follow [AGENTS.md](../AGENTS.md) for Python/test style and
[CONTRIBUTING.md](../CONTRIBUTING.md) for the contribution workflow. Use concise
Conventional Commits such as `fix(arena): preserve outcome after timeout`.

When changing a behavior, update its relevant guide as well as the implementation.
Preserve known-answer shuffle vectors and legacy log fixtures unless deliberately
changing compatibility. Keep tests and comments understandable without historical
planning documents. Add configuration where it belongs: game shape in
`GameRules`, experiment protocol in `MatchProtocol`, operational bounds in
`RunConfig`, external lifecycle policy in `SessionOptions`, and strategy settings in the bot's `BotOptions` subclass.

The public application workflow has one execution path. Do not add a parallel
runner for presentation or seat type: extend common planning, capability checks,
seat preparation, or observer hooks as appropriate. `run_match` in `arena.match`
remains a low-level primitive for directly supplied bot instances.

A caller-provided sink remains caller-owned. Matches close sinks they create;
workers close their per-match resources. Timed-out calls retain their bot until
they finish, and late actions are discarded. Collected outcomes and run status
preserve available evidence when execution stops. Do not let cleanup errors
replace an established game outcome.

## Ownership and type boundaries

- Keep engine rules and transitions independent of arena, storage, and presentation.
- `RunSettings` is the input boundary. Resolve its execution policy into `MatchLimits` and `RunLimits`; derive runtime `RecordingPolicy` from common recording options.
- `SessionOptions` owns readiness, invocation, retention, and notebook settings. It does not change gameplay deadlines.
- Every job supplies explicit match and bot seeds from the same versioned planner. Preserve existing sampled seed vectors and recorded replay fixtures.
- `DecisionMetrics` records attempted calls independently of traces. Bots provide `CandidateEvaluation` for optional presentation; cumulative `stats()` is collected separately.
- An owned bot is closed once even when construction or startup fails. A never-started bot receives `close(None)`. Cleanup diagnostics do not replace an established match outcome.
- `validate_run_evidence` checks identities and accounting before loading or analysing outcomes. Formatting consumes shared report projections.

`tests/test_architecture.py` checks the core import boundaries. Selected strict
`ty` rules reject missing generic parameters and unsound returns/assignments in
source, tests, and experiments. `scripts/check_typing.py` checks both accepted
calls and intentionally invalid examples, including exact diagnostic locations;
its `.py.txt` fixtures are never executed.

Use `JsonValue` for extensible serialized data and precise models or closed
`TypedDict` variants for internal messages. Validate untrusted values once before
using them as typed objects. Keep explicitly dynamic third-party/legacy adapters
small; do not hide invalid calls with a cast or a blanket suppression.
