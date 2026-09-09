# Architecture and development

## Source map

| Package/module | Responsibility |
| --- | --- |
| `engine/cards.py` | Deck, penalty values, seed derivation, shuffle, deal |
| `engine/rules.py`, `state.py`, `actions.py`, `events.py` | Typed game configuration, state, actions, and events |
| `engine/setup.py`, `transition.py`, `resolution.py`, `lifecycle.py` | Match setup and legal game transitions |
| `engine/audience.py`, `fold.py`, `views.py` | Audience filtering and incremental player observations |
| `engine/replay.py` | Reconstruct authoritative state from recorded events |
| `arena/runner.py`, `scheduling.py`, `config.py`, `results.py` | Offer decisions, schedule seats/matches, enforce limits, aggregate results |
| `arena/decisions.py`, `transactions.py` | Bot deadlines and validation of atomic proposals |
| `arena/players.py`, `bots/` | Per-seat configuration, registry, strategies, LLM adapters |
| `arena/tracing.py`, `persistence/` | Experiment provenance and durable JSONL/manifest output |
| `analytics/summary.py` | Metrics derived from logs |
| `common/text.py` | Shared text validation |
| `cli.py` | `arena`, `replay`, and `summarise` commands |

The engine owns rules and hidden state. It does not call bots, write files, or
invoke model providers. The arena drives the engine and owns experiment
scheduling and failure policy. Persistence depends on engine models; it does not
choose moves. Analytics derives metrics from recorded history.

Arena runs select a thread pool or a spawned process pool through `RunConfig`.
Both use bounded submission and the same aggregation and failure policy. Process
workers receive resolved player factories once at initialization, build fresh
bots per match, and return compact summaries. Trace writing stays with each match
worker; manifest writing stays in the parent. Deadline accounting uses shared
run-wide counters in process mode. Engine transitions remain synchronous within
each match, and process workers do not accept live observer callbacks.

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

Use `uv` for the environment and all Python commands:

```bash
uv sync --all-groups
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

The default pytest configuration excludes `arena_slow`. Run only the longer
tests or explicitly run every test with:

```bash
uv run pytest -m arena_slow
uv run pytest -o addopts=''
```

The volume suite checks 1,000 matches at each of 2, 3, 5, and 10 players in both
classic and communication modes. It validates intermediate card conservation,
row placement, scoring, private observations, and replay. These tests can take
tens of minutes; use focused tests while iterating.

For process-backend changes, the focused volume checks compare 100 mixed-baseline
matches per communication mode against thread execution and replay every process
trace:

```bash
uv run pytest tests/test_process_arena.py -m arena_slow
```

Tests are grouped by observable behavior: card/dealing vectors, model validation,
resolution/termination, audience/view history, replay, arena limits and
concurrency, configuration, CLI, and LLM parsing/memory. LLM tests use controlled
HTTP transports with the real SDK rather than contacting providers.

## Making changes

Follow [AGENTS.md](../AGENTS.md) for Python/test style and
[CONTRIBUTING.md](../CONTRIBUTING.md) for the contribution workflow. Use concise
Conventional Commits such as `fix(arena): preserve outcome after timeout`.

When changing a behavior, update its relevant guide as well as the implementation.
Preserve known-answer shuffle vectors and legacy log fixtures unless deliberately
changing compatibility. Keep tests and comments understandable without historical
planning documents. Add configuration where it belongs: game shape in
`GameRules`, experiment protocol in `MatchProtocol`, operational bounds in
`RunConfig`, and strategy settings in the bot's `BotOptions` subclass.
