# Phase 7 — The arena as an experiment platform

## What this document is

An implementation brief. It is self-contained: you should be able to work from
this file plus the codebase, without reading the review history that produced it.
Where it depends on a rule from `IMPLEMENTATION_SPEC.md`, it states the rule and
cites the section so you can check it, rather than assuming you have read it.

`IMPLEMENTATION_SPEC.md` §16 is the specification this plan implements. If the
two ever disagree, the spec wins and this file is wrong.

Per `AGENTS.md`: **the specs are for you, not for the codebase.** Code, tests,
comments and commit messages must stand on their own. Never write "as required by
D4" or "see §16.3" in a comment — say what the code does and why, so a reader who
has never seen these documents understands it.

## What you are building

The project has one rules engine for the card game 6 nimmt! and two surfaces that
drive it:

- **The server** (`server/`) hosts matches over HTTP for outside clients.
- **The arena** (`arena/`) runs matches in-process, with no HTTP, so bot line-ups
  can be played in volume and studied.

The server is done. The arena is not: it is a classic-only match driver, written
to prove the engine correct at volume, and it does that well. You are turning it
into an experiment platform — one that runs both game modes, admits bots of any
kind including LLM-backed ones, tolerates the ways those fail, and writes traces
you can analyse afterwards.

**Assume LLM-backed bots.** You are not writing one — that needs an API client,
credentials and a prompt surface, none of which is arena machinery — but almost
every non-obvious decision here exists because a seat might be a model: slow,
occasionally broken, expensive, and bad at responding to "no" without being told
why. A design that only suits scripted bots would be much smaller and wrong.

---

## Orientation

### Layout

```
src/sixnimmt_server/
  common/text.py      shared validation of client-supplied text
  engine/             the rules. Pure, no I/O, no clock, no network
    cards.py          deck and bull-head values
    state.py          MatchState, PlayerSeat, RowState, ResolutionState (frozen)
    rules.py          GameRules, MatchProtocol, InformationPolicy
    actions.py        the typed Action union
    events.py         the typed Event union
    transition.py     transition(state, player_id, action, protocol, rules)
    resolution.py     card placement and the row-choice pause
    audience.py       Viewer, visible_to, visible_events — who may see an event
    views.py          MatchView and the other view types
    fold.py           build_view(events, viewer) -> MatchView
    replay.py         a whole log -> final state
    setup.py          open_match, create_match, dealing
    lifecycle.py      hand and play boundaries, score banking
  server/             HTTP: app, routes, auth, store, sink
  arena/              runner.py, bots.py
  cli.py              typer CLI: arena, serve, replay
tests/                pytest, one file per area
```

### Running things

```
make check     # ruff via pre-commit, uv lock check, ty type check
make test      # pytest with coverage
uv run pytest tests/test_arena.py -x          # one file
uv run pytest -m arena_slow                    # the volume suite, excluded by default
```

Python 3.13, `uv` for everything, pydantic v2, pytest. `ty` is the type checker.
Commit messages are one-line Conventional Commits: `<type>(<scope>): <description>`.

### The rules that constrain every choice here

These come from the spec and are not negotiable. They are the reason several
decisions below look more elaborate than the task seems to need.

**The engine is pure** (§3). No I/O, no network, no clock, no ambient randomness.
`transition(state, player_id, action, protocol, rules) -> (new_state, [events])`,
or it raises `EngineRejection` and changes nothing. Surfaces drive; they never
decide. If a surface can answer a question about the game that the engine could
not, that is a defect.

**Everything that happens is an event, and every event carries an audience**
(§3, §10.1). State is the fold of the event log. `audience` is `public`,
`player:<id>`, or `admin`, and it is the *only* mechanism preventing information
leaks — not per-endpoint filtering.

**A viewer's state is built by folding only the events they may see** (§9.2).
This is the rule most easily broken by convenience. Do not project `MatchState`
and blank the private fields: that passes every test written against the fields
you happened to copy, and leaks the first field somebody adds later.

**Layering**, enforced by `tests/test_engine_boundaries.py`:

| Package | May import |
|---|---|
| `common/` | nothing of ours |
| `engine/` | `common/` |
| `persistence/` | `common/`, `engine/` |
| `arena/` | `common/`, `engine/`, `persistence/` |
| `server/` | `common/`, `engine/`, `persistence/` |

`arena/` must never import `server/`. The arena is the fast in-process path and
pulling a web framework into it defeats the purpose.

### Two things about the game itself

**Phases.** `SETUP -> SELECTING -> RESOLVING -> (next play | next hand | FINISHED)`,
with `AWAITING_ROW_CHOICE` hanging off resolution. During `AWAITING_ROW_CHOICE`
only one named player may act, and only `choose_row`; everything else is rejected
with `NOT_YOUR_TURN` (§8).

**Two modes, one engine.** Classic is `negotiation_enabled = false`: selecting a
card commits it, `uncommit` is illegal, messaging is off. Negotiation allows
messages, re-selection and uncommitting during `SELECTING`, and the play proceeds
only when every player has committed. This matters constantly below, because
classic has exactly one player who can act at any moment and negotiation has no
such thing.

---

## Where the arena is today

Verified against this checkout. Read `arena/runner.py` and `arena/bots.py` before
starting; they are 240 lines together.

- `run_match` (`runner.py:103`) hardcodes `GameRules()` and `MatchProtocol()` at
  lines 122–123, so every arena match is classic with default rules.
- `_acting_seat` (`runner.py:62`) returns the first uncommitted seat and the loop
  takes exactly one action from it. Under negotiation there is no such seat.
- `observe_player` (`runner.py:75`) hand-builds a `BotObservation` from
  `MatchState`: hand, rows, hand and play numbers. No scores, piles, commitment
  flags or messages.
- `_bot_action` (`runner.py:88`) narrows the bot's return type to
  `SelectCardAction | ChooseRowAction`.
- The `except Exception` at `runner.py:138` turns **every** `EngineRejection`
  into a fatal `ArenaError` that ends the whole run.
- `run_arena` (`runner.py:149`) accepts only the bot name `"random"` (line 167)
  and keeps per-seat counters. Nothing writes a log.
- `create_match` (`setup.py:201`) already accepts `PlayerSeat` objects and both
  `rules` and `protocol`. Nothing in the engine blocks any of this.
- `build_view(events, viewer)` (`fold.py:302`) already builds the player view
  from an audience-filtered stream, in `engine/`, with no server dependency.
- `fold.py` does **not** handle `action_rejected`. A rejection advances the
  offending player's cursor and changes nothing else they can see. Correct for
  the server, which puts the refusal in the HTTP error body — and the reason D4
  exists.
- `store.py:346` writes the `ActionRecord` **before** the version check and
  before the transition, so a rejected server action consumes a
  `server_action_seq` and is recorded.
- `action_rejected` events carry `{code, message, action_type}` in `data`
  (`store.py:439`).
- `EventSink`, `JsonlEventSink`, `ActionRecord`, `read_event_log` and
  `read_action_log` live in `server/sink.py`, alongside `LiveEventStream` and
  `Subscription` (asyncio fan-out for long polls and SSE).
- `ActionRecord.action_id` is required (`sink.py:53`); `Action.action_id` is
  optional and defaults to `None` (`actions.py:30`).

---

## D0 — Bots observe `MatchView`

**Delete `BotObservation` and `observe_player`. A bot receives the `MatchView`
built by `build_view(events, viewer)` — the same object, from the same code, that
an HTTP client gets from `GET /state`.**

`observe_player` is the projection-and-blank pattern §9.2 forbids. It is safe
today only because somebody picked safe fields, and it must grow for negotiation
— messages, commitment flags, scores, piles — which is exactly the growth during
which such a thing leaks. Folding instead makes the arena's information
discipline structural, and lets the existing §5 leak tests cover arena seats
without being rewritten.

**Consequence: the arena must keep an event log.** It currently keeps only
`MatchState`. Append every batch returned by `transition()` to a per-match list
and fold views from it. That list is also what D8 persists, so it is one
mechanism serving both needs.

`RowView` stays. The `decision` field disappears with `BotObservation`: a bot
reads `view.phase` and `view.legal_actions`, exactly as an HTTP client does.

## D1 — Views fold incrementally

Re-folding a viewer's whole visible stream on every decision is quadratic in
match length. A 10-player match is roughly 1 000 accepted actions and several
thousand events; at 1 000 games that is billions of event applications, against a
volume suite that currently finishes.

**Add a live folder to `fold.py`:**

```python
class ViewFolder:
    def __init__(self, viewer: Viewer, *, explicit_counts: bool = True) -> None: ...
    def apply(self, events: Sequence[Event]) -> None: ...  # filters, then folds
    def view(self) -> MatchView: ...
```

`build_view` becomes a thin wrapper — construct, apply everything, project — so
there is one fold implementation and the two cannot disagree. Assert that they do
not, after every appended batch.

**`explicit_counts` is a parameter, not a pre-scan.** `build_view` currently
decides a legacy-log compatibility mode by scanning the whole sequence for
`action_counted` events (`fold.py:305`). A live folder cannot do that, not having
seen the future, and does not need to: a live match is written by the current
version and always has those markers. Whole-log reads keep the pre-scan; the live
folder is told. Comment which caller each mode serves.

This also fixes the server: `store.py` rebuilding a view per action is why a
message-spamming agent could make it quadratic. The store adopts the same folder
(commit 3), so the fix lands on both surfaces.

**Do not fold rejections into the view.** It is tempting to make `action_rejected`
appear in `MatchView` and use that as D4's feedback channel. It is the wrong
place: a view is a projection of the match, a rejection is a fact about one
attempt, and adding it would change what every HTTP caller receives from
`/state` for a reason that has nothing to do with them.

## D2 — Rules, protocol and seats are run parameters

`run_match` and `run_arena` take `rules: GameRules` and `protocol: MatchProtocol`.
Seats become `PlayerSeat`s carrying `display_name` and `agent_metadata` — which
`create_match` already accepts — and the arena fills the metadata from the bot's
registry entry (D7). Player ids stay `player_1 … player_n`, so a seat means the
same thing across every game in a run.

**No new game-configuration type.** Anything that changes what is legal, what is
visible, or how a match ends belongs in `GameRules`/`MatchProtocol`, where the
server can express it too; a rule that existed only in the arena would produce
results that say nothing about real play. The scheduler, the action limits and
the concurrency are *harness* settings, not rules — they decide who is offered a
turn and when the arena gives up — and they live in `RunConfig` and the manifest.

## D3 — Scheduling

Classic has no scheduling problem: one seat can act and it is the one the engine
is waiting on. Negotiation has no "whose turn it is" — during `SELECTING` every
uncommitted player may act, repeatedly, in any order — so the arena must choose
who is *offered* a turn, and that choice is part of the experiment.

New `arena/scheduling.py`:

```python
class Scheduler(Protocol):
    def next_seat(self, state: MatchState) -> int | None: ...  # SELECTING only
```

- **`SequentialScheduler`** — the classic rule: the first seat that has not
  committed. **Valid only when `negotiation_enabled` is false.** Under
  negotiation a seat that never commits stays the first uncommitted seat forever,
  so every later seat starves and the play never reaches unanimity. Refuse this
  combination when the run is configured, naming the starvation — a starved run
  looks exactly like a slow one, and you will not diagnose it later.
- **`RoundRobinScheduler`** — during `SELECTING`, cycle over uncommitted seats,
  resuming after the seat offered the last turn. Skip committed seats until
  somebody uncommits. Valid in both modes; in classic it degenerates to
  `sequential`, because there selecting is committing.

**A scheduler instance belongs to one match** and is single-threaded, so "the seat
offered the last turn" cannot leak between games or be shared by concurrent ones.

**`AWAITING_ROW_CHOICE` bypasses the scheduler** in both modes: the engine names
the awaited player and only that player may act. Put this branch in the runner,
not in each scheduler, so a future scheduler cannot get it wrong. `next_seat` is
only ever called during `SELECTING`.

Default: `round_robin` under negotiation, `sequential` otherwise, resolved from
the protocol (see `resolve()` in the pinned interfaces).

The scheduler decides who is offered a turn. It never decides legality.

## D4 — Rejections explain themselves; failures are classified

The current `except Exception` conflates several unrelated things.

### An `EngineRejection` is retried, with the reason

`MatchProtocol.on_invalid_action = "reject"` means a rejected action is rejected,
counted and retried, and never costs the turn. That is a rule about matches, not
about transport, so it binds the arena exactly as it binds the server. Catch the
rejection, append an `ActionRejectedEvent` addressed to `player:<seat>` — the
same type `store.py:439` builds — and ask the seat again.

**The retry must tell the bot what was wrong.** This is the part that is useless
to get wrong quietly. `fold.py` does not surface `action_rejected`, so a seat
re-asked with only a refreshed view is looking at materially the same thing that
produced the illegal action. A model will produce it again until its budget is
spent. So pass the refusal back:

```python
@dataclass(frozen=True)
class Rejection:
    code: ErrorCode
    message: str
    legal_actions: tuple[str, ...]
```

This is not a new channel — it is what the server already returns in the body of
a 4xx for the identical refusal (§9.4). Assert that the two agree, against the
server's own error body, rather than against a copy of it. `message` comes from
the `EngineRejection`; `legal_actions` from the freshly folded view.

It deliberately omits `view_version`, which §9.4 also carries. Over HTTP the
error body is all a caller gets, so it must carry the cursor; here the refreshed
`MatchView` goes to the bot alongside the rejection and already has
`view_version` in a field. Two sources for one number is worse than one.

### What a decision is

`decision_rejection_limit` (default 8) bounds the retry loop, and "per-decision"
must be defined or it bounds nothing.

**A decision is one offer from the runner.** It begins when the runner offers a
seat a turn — whether the scheduler chose that seat, or the phase forced it, as
`AWAITING_ROW_CHOICE` does — and ends when the seat's action is accepted or the
seat gives up. The counter resets on the next offer and on any accepted action.

Defining it as a *runner* offer rather than a *scheduler* offer matters: the
scheduler is bypassed during `AWAITING_ROW_CHOICE`, and without this an illegal
row choice would have no rejection budget at all.

A seat may therefore spend its whole budget again on every offer. That is
intended — a model that fumbles one play should not carry a penalty into the next
— and the totals are bounded by `play_action_limit` where it is set and by
`match_action_limit` always (D5).

Exhausting the budget **forfeits**: append `match_abandoned`, end with outcome
`forfeited`, record the seat and the last error code.

### Which failures a match may absorb, and which it may not

Not all failures are equivalent, and treating them alike would either lose
matches to bookkeeping errors or hide broken experiments as ordinary results.

| Failure | Consequence |
|---|---|
| `act` raises, returns a non-`Action`, or exceeds its deadline (D9) | that match ends `failed`; the run continues |
| `BotSpec.build` raises **on the first game** | run fails: the line-up cannot be constructed at all |
| `BotSpec.build` raises **on a later game** | that match ends `failed`; builds have worked before, so this is transient — a credential refresh, a rate limit — not a broken line-up |
| writing a trace or the manifest fails, in a traced run | run fails: an experiment that is not being recorded is not running. Surface it as the run's error; the manifest is exactly what is unavailable |
| a scheduler raises | run fails: our defect, not a result |
| `stats()` raises | the manifest records that the seat reported none, and why; nothing else changes |

The last row matters: losing a cost figure must never cost a match.

A raised exception is **not** retried. Record the seat, game index, seed,
exception type and message so it can be reproduced, and continue the run — a
twelve-hour experiment must not end because one provider had a bad minute, and a
run that stops silently at game 40 of 1 000 is worse than one recording 12
failures. `stop_on_failure` (default false) makes it stop instead, which is what
a developer debugging a scripted bot wants (D9).

### A bot returning a non-`Action`

Validate the return in the runner. `transition()` type-checks nothing and would
fail on attribute access, so an unvalidated bad return surfaces as an
`AttributeError` from inside the engine, naming the wrong culprit.

## D5 — Termination

Three limits, all in the runner, none in the engine. They are named for what they
do to a match rather than for what they count, so that no flag has to
disambiguate them from `MatchProtocol.max_actions_per_play`.

| Setting | Default | Counts | Resets | Effect |
|---|---|---|---|---|
| `match_action_limit` | 10 000 | attempts, accepted and rejected alike | never | match abandoned |
| `play_action_limit` | `None` classic, 200 negotiation | attempts in the current play | on `play_started` | match abandoned |
| `decision_rejection_limit` | 8 | consecutive rejections in one decision | every new offer, any accepted action | seat forfeits (D4) |

**They count attempts, not accepted actions.** What they bound is an agent doing
work, and a rejected action is work — a model call, a transition, a log record.
Counting only accepted actions would let a seat that never produces a legal
action run inside its play forever, which is the adversary they exist for.

**When to check, and what wins.** Evaluate limits after applying a transition and
before offering the next decision, in this order:

1. If the batch contains `match_ended`, the match is **`finished`**. A match the
   engine has completed is never reported as abandoned, whatever the counters say.
2. If the batch contains `play_started`, reset the play counter first.
3. Rejection budget → forfeit.
4. `play_action_limit` → abandon.
5. `match_action_limit` → abandon.

Most specific wins, and a forfeit names a responsible seat where an abandonment
does not: "player_3 kept playing cards it does not hold" is a better experimental
record than "the match stopped". `MatchResult.reason` records which fired.

**A failed decision consumes no limit.** It ends the match immediately, and there
is no longer a match for a limit to bound.

`MatchProtocol.max_actions_per_play` is a different thing and both may be in
force: it is a **game rule**, enforced by the engine per player, rejecting with
`ACTION_BUDGET_EXHAUSTED`. The arena's `play_action_limit` is a **harness limit**
across all seats in a play, and it abandons rather than rejects.

**`MatchOutcome` is `finished | abandoned | forfeited | failed`**, counted
separately. Only a `finished` match contributes a win, a tie or a score to the
aggregates. Rolling any other into them would let a scheduling bug or a flaky
provider read as a strategy result.

## D6 — Persistence moves to `sixnimmt_server/persistence/`

Move `EventSink`, `JsonlEventSink`, `AtomicJsonlWriter`, `ActionRecord`,
`read_event_log`, `read_action_log`, `event_log_path`, `action_log_path` and
their helpers from `server/sink.py` to `persistence/sink.py`. Leave
`LiveEventStream`, `Subscription` and `SinkClosed` — asyncio fan-out, HTTP-only —
behind in `server/stream.py`.

The arena writes the same traces as the server and must not import `server/`, so
persistence has to sit below both. That is what makes "one trace format"
structural rather than aspirational: one writer, and neither surface has a
private one.

Extend `tests/test_engine_boundaries.py` to assert the whole layering table, not
just the engine row. The existing text scan is crude but it has held.

## D7 — Bot registry, metadata, statistics, strategies

```python
@dataclass(frozen=True)
class BotSpec:
    name: str
    build: Callable[[int], Bot]  # seed -> bot; one instance per match
    deterministic: bool
    metadata: dict[str, Any]  # -> PlayerSeat.agent_metadata
```

`REGISTRY: dict[str, BotSpec]` in `arena/bots.py` is the only place a name maps
to an implementation. Resolve every name before the first game so an unknown one
fails immediately, listing what exists — which is what `run_arena` does today.

**Bots may report opaque statistics.** An experiment against models is
uninterpretable without knowing how many calls a seat made, how long they took
and what they cost, and the arena must not learn what a token is. So `Bot` gains
one optional method:

```python
def stats(self) -> dict[str, Any]: ...  # optional; recorded verbatim, never interpreted
```

Recorded per seat per match into the manifest, treated exactly as
`agent_metadata` is — opaque, never parsed. A bot with none, and a bot whose
`stats()` raises, are both recorded as having reported none, the latter with its
exception.

Two bots ship:

- **`random`** — today's `RandomBot`, ported to the view interface: it selects
  from `view.you.hand` and chooses from `view.rows`. Under negotiation it must
  also **commit**: select if it has no selection, commit otherwise. A bot that
  only ever selects would abandon every negotiation match and leave the mode
  untested by its own volume suite.
- **`greedy`** — a scripted strategy: play the card that would take the fewest
  bull heads against the current board; on a row choice, take the row with the
  fewest heads; break ties toward the lower card and the lower row index. **It
  holds no RNG**, so `deterministic: true` is honest. Under negotiation it
  commits its choice immediately and sends no messages. Its quality as a player
  is not a goal — admitting real strategies through the interface is.

`deterministic` is declared metadata, not something the arena can verify. A run
whose seats are all deterministic is reproducible and the manifest says so; one
containing any non-deterministic seat is not, and the manifest says that instead.
This is the seam an LLM bot plugs into.

**What an LLM bot will need from you, all of it specified here:** the full view,
the full action union, rejections that explain themselves, `agent_metadata`,
`stats()`, concurrency, a decision deadline, survival of its own failures, and
honest reproducibility reporting.

## D8 — Traces, action records, the manifest

Tracing is opt-in per run: one log per game for a million games is not useful,
and the aggregate counters must not depend on it. **A run without `trace_dir` is
a smoke test, not an experiment** — it produces a scoreboard and no provenance.
Say so in the CLI help, and set a trace directory for anything with an LLM seat.

With `trace_dir` set, each match writes `{run_id}_{game_index}.jsonl` and its
action records through `JsonlEventSink`.

**Action records cover every attempted action, accepted or rejected.**
`server_action_seq` is assigned per attempt in submission order, and a rejected
attempt consumes one — matching §9.5 step 3 and `store.py:346` exactly. The
difference between the action log and the event log is then the record of what
agents proposed and were refused, which is data an experiment wants. (A cached
idempotent replay on the server is *not* a second attempt and gets no record;
the arena has no such case.)

Two fields the arena supplies that a client would:

- **`action_id`** = `{match_id}:{server_action_seq}`. `ActionRecord.action_id` is
  required and an arena bot has no reason to invent one. Deterministic, so it
  does not perturb reproducibility.
- **`from_view`** = the `view_id` of the view the arena actually handed the bot,
  which it knows for certain, having built it. The server can only record what a
  client claimed, so this is the better audit reference of the two.

**Decision timestamps, because latency is otherwise not derivable.** `received_at`
is when an action *arrived* — after the thinking finished — so deltas between
consecutive records measure submission gaps and include the previous transition.
For scripted seats nobody noticed; for LLM seats latency is the headline number.
`ActionRecord` therefore gains:

- `decision_started_at: datetime | None` and `decision_ended_at: datetime | None`
  — wall-clock, for consistency with `received_at`;
- `decision_duration_ms: float | None`, measured with `time.monotonic()`, because
  a clock adjustment can make the wall-clock difference negative;
- `outcome: Literal["accepted", "rejected", "timeout", "error"]`;
- `type: str | None` — `None` when no action was returned at all.

The arena fills all of these. The server leaves the timing fields unset, because
a client's thinking happens across a socket and anything it reported would be a
claim rather than a measurement, and always sets `outcome` to `accepted` or
`rejected` and `type` to a real value. Nothing in a transition or a replay may
read any of them: they are analytics inputs, and a match must fold identically
with them absent.

**A decision that never returns still gets a record.** A timeout or an exception
produces no action, so without this the one thing worth knowing — which view the
model had and how long it ran before you gave up — is lost. Write a record with
`type: None`, the appropriate `outcome`, `from_view` set to the offered view, and
the timing fields filled.

**Do not store the `Rejection` payload or a view snapshot.** It is reconstructible:
`action_rejected` already carries `{code, message, action_type}` in its `data`
(`store.py:439`), and `legal_actions` is a pure function of the folded view at
the cursor `from_view` names. Storing it again is duplication that can drift.

**`run_id` collisions.** `arena_{utc second}_{seed}` can repeat, and
`AtomicJsonlWriter` opens in append mode (`sink.py:209`), so two runs would
interleave into one file. Create the trace directory **exclusively**: a run whose
directory exists refuses to start. A refusal is recoverable; a silently merged
log is not.

The manifest is pinned below. It carries the line-up and metadata, the resolved
rules and protocol, the run config, the root seed, and per match the derived
seed, the outcome, the forfeiting or failing seat with its reason, and per-seat
statistics.

## D9 — Concurrency, deadlines, and stopping

A scripted seat answers in microseconds; a model answers in seconds. A thousand
games of a thousand decisions at two seconds each is not a run anybody waits for.

**Matches run concurrently; a match runs strictly sequentially.** Actions within
a match are ordered and that ordering is the experiment, so nothing inside a
match becomes concurrent. Whole matches do. Each already has its own derived seed
and its own bot instances, so concurrency changes no match's result — assert
that, do not assume it.

- `concurrency: int = 1`, so today's behaviour is the default and scripted volume
  runs are unchanged.
- Threads, not asyncio: `act` is a blocking call into arbitrary bot code, the
  engine is synchronous, and LLM work is I/O-bound. Threads cost nothing here and
  impose no colour on the `Bot` protocol; an async bot wraps its own event loop.
- **One bot instance and one scheduler per match**, never shared across
  concurrent matches. Whatever a bot shares underneath — an HTTP client, a rate
  limiter — is the bot's to make safe; the arena guarantees only that it will not
  call one instance from two matches.
- Submit matches as capacity frees, at most `concurrency` in flight. Do not
  submit every game up front: it materialises a million futures on a million-game
  run, and it lets queued work start after a stop has been requested.
- Aggregate as matches complete, keyed by game index, so counters do not depend
  on completion order.

### The decision deadline

Without one, concurrency is worse than none: D5's limits are checked *between*
decisions, so a provider call that never returns is never checked at all.

`decision_timeout_seconds: float | None = None`. **How it is executed matters,
and there is exactly one arrangement that works:**

- **When it is `None`** (the default, and every scripted run): call `act` inline
  on the match's own thread. No extra machinery, no cost.
- **When it is set:** run the decision on a **separate daemon thread**, and have
  the match's own thread wait on it with a timeout. On expiry the match thread
  stops waiting, records the timeout (D8), ends the match `failed` with reason
  `decision_timeout`, and moves on.

The match thread must not be the one making the call. If it were, a hung `act`
would block the very code that has to mark the match failed, append its terminal
events and write its trace — the match could never be finalised, which is the
trap the obvious implementation falls into.

Two consequences to implement rather than discover:

- **The call is not cancellable.** A blocking call in a Python thread cannot be
  interrupted from outside it, so the abandoned thread runs until it returns on
  its own, if it ever does. Its result is discarded. It must be a **daemon**
  thread, or the interpreter cannot exit while a call is hung — `ThreadPoolExecutor`
  joins its workers at shutdown, which is why decisions do not use one.
- **Abandoned threads accumulate**, so bound them: `max_abandoned_decisions`,
  default `4 * concurrency`. Exceeding it stops the run and says so. Note that
  match *capacity* is never lost — the match thread always returns — so this is a
  guard against a thread leak, not against deadlock.

The real fix belongs to the bot: a seat calling a model must impose its own
client-side timeout. The arena's deadline is a backstop that keeps the run alive;
it is not a resource guarantee, and the code comments should say so.

### Stopping

`stop_on_failure` cannot mean "stop now" on a pool of blocking calls.

On a failure: stop submitting, drop what has not started, and let what is already
running finish and record its outcome. A match killed halfway is not a result and
writing one into the manifest would be a fiction.

**Draining is bounded only if `decision_timeout_seconds` is set.** With no
deadline, an in-flight hung seat blocks the drain indefinitely. That is a real
limitation, not a bug to design around — it is why an LLM run should always set a
deadline, and the CLI should say so when `--stop-on-failure` is used without one.

Report `games_requested`, `games_started` and `games_completed`. They are equal
on a run that submits everything, and differ when stopping or a run-fatal error
curtailed submission. They are not a failure detector: ordinary `act` failures
still complete their matches and leave all three equal.

## D10 — `MatchSummary`, so traces have a reader

The rest of the analytics work is phase 5. The minimum ships here, because traces
nothing reads are just disk.

`analytics/summary.py` folds one event log plus its action records into a
`MatchSummary`: per-seat final and per-hand scores, wins, actions attempted and
rejected, messages sent and received, and decision latency from D8's timestamps
where the writing surface could measure them. It does not know which surface
wrote a log — prove that by running it over one arena log and one server log.

**It also takes the match's manifest entry, optionally.** `abandoned`,
`forfeited` and `failed` all end with `match_abandoned` in the event stream, and
the responsible seat and reason live only in the manifest — so without the entry
the summary cannot tell the three apart.

```python
def summarise(
    events: Sequence[Event],
    actions: Sequence[ActionRecord],
    manifest_entry: ManifestMatch | None = None,
) -> MatchSummary: ...
```

Given the entry, report the true outcome and who caused it. Without it, report
`abandoned` **and say there was no manifest**, rather than implying you knew.
`ManifestMatch` carries its own `manifest_version` — denormalised from the
manifest root — so this signature can refuse a version it does not understand
instead of misreading it.

The alternative, a terminal event carrying the reason, was rejected:
`match_abandoned` is a `public` event in §10.3's catalogue, and putting "player_3
kept playing cards it does not hold" into a public event would push an arena
convenience into the server's information contract. The manifest is the arena's
own record and has no audience problem.

## D11 — Command line

`sixnimmt arena` gains `--negotiation`, `--scheduler`, `--trace-dir`,
`--concurrency`, `--decision-timeout`, `--play-action-limit`,
`--match-action-limit`, `--decision-rejection-limit`, `--max-abandoned-decisions`
and `--stop-on-failure`. Existing flags keep their behaviour, and
`PlayersCommand`'s repeated/contiguous `--players` parsing is untouched.

`--scheduler` is optional and resolves from the protocol when omitted.
**Validation lives in `run_arena` and `run_match`, not in the CLI**, so a
programmatic caller cannot construct the starving combination either.

Add `sixnimmt summarise <log>`, alongside the existing `replay`.

Output reports finished, abandoned, forfeited and failed counts separately. A run
that abandoned 400 of 1 000 games while printing a clean scoreboard is the exact
failure the outcome vocabulary exists to prevent.

---

## Pinned interfaces

Resolved here so you invent nothing.

```python
# arena/bots.py
class Bot(Protocol):
    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action: ...
    def stats(self) -> dict[str, Any]: ...  # optional


@dataclass(frozen=True)
class Rejection:
    """Why the last action was refused: §9.4's payload, minus the transport."""

    code: ErrorCode
    message: str
    legal_actions: tuple[str, ...]


# arena/scheduling.py — one instance per match, single-threaded
class Scheduler(Protocol):
    def next_seat(self, state: MatchState) -> int | None: ...  # SELECTING only


# arena/runner.py
class MatchOutcome(StrEnum):
    FINISHED = "finished"
    ABANDONED = "abandoned"  # an action limit was reached
    FORFEITED = "forfeited"  # a seat exhausted its rejection budget
    FAILED = "failed"  # a bot raised, timed out, or returned a non-Action


@dataclass(frozen=True)
class RunConfig:
    """Harness settings. Never game rules — those are GameRules/MatchProtocol."""

    scheduler: str | None = None  # None until resolved against the protocol
    match_action_limit: int = 10_000
    play_action_limit: int | None = None  # resolved to 200 under negotiation
    decision_rejection_limit: int = 8
    decision_timeout_seconds: float | None = None
    max_abandoned_decisions: int | None = None  # default 4 * concurrency
    concurrency: int = 1
    stop_on_failure: bool = False
    trace_dir: Path | None = None


def resolve(config: RunConfig, protocol: MatchProtocol) -> RunConfig:
    """Fill mode-dependent defaults, then validate.

    Called by run_arena and run_match, so no caller can reach the runner with
    sequential scheduling on a negotiation match.
    """


@dataclass(frozen=True)
class MatchResult:
    seed: int
    outcome: MatchOutcome
    final_state: MatchState
    events: tuple[Event, ...]
    winners: tuple[str, ...]  # empty unless outcome is FINISHED
    actions_accepted: int
    actions_rejected: int
    ended_by: str | None = None  # the forfeiting or failing seat
    # Which limit fired, the last ErrorCode, "decision_timeout", or an
    # exception repr — whichever ended the match.
    reason: str | None = None


@dataclass(frozen=True)
class SeatResult:
    player_id: str
    bot_name: str
    wins: int
    ties: int
    total_score: int  # over FINISHED matches only
    actions_accepted: int
    actions_rejected: int


@dataclass(frozen=True)
class ArenaResult:
    run_id: str
    seed: int
    games_requested: int
    games_started: int
    games_completed: int
    finished: int
    abandoned: int
    forfeited: int
    failed: int
    decisions_abandoned: int  # daemon threads left running by a timeout
    total_hands: int
    total_actions: int
    reproducible: bool
    players: tuple[SeatResult, ...]


# persistence/sink.py — fields added to the shared record
class ActionRecord(BaseModel):
    server_action_seq: int
    action_id: str
    player_id: str
    type: str | None  # None when no action was returned
    from_view: str | None
    received_at: datetime
    outcome: Literal["accepted", "rejected", "timeout", "error"] = "accepted"
    decision_started_at: datetime | None = None  # arena fills; the server cannot
    decision_ended_at: datetime | None = None
    decision_duration_ms: float | None = None  # monotonic, not a clock difference


# analytics/summary.py
def summarise(
    events: Sequence[Event],
    actions: Sequence[ActionRecord],
    manifest_entry: ManifestMatch | None = None,
) -> MatchSummary: ...
```

`ViewFolder` as given in D1.

Manifest, written once per traced run:

```json
{
  "manifest_version": 1,
  "run_id": "arena_20260906T142200Z_1234",
  "seed": 1234,
  "games_requested": 1000,
  "games_started": 1000,
  "games_completed": 1000,
  "reproducible": true,
  "rules": { },
  "protocol": { },
  "run_config": {"scheduler": "round_robin", "match_action_limit": 10000,
                 "play_action_limit": 200, "decision_rejection_limit": 8,
                 "decision_timeout_seconds": 120, "concurrency": 8,
                 "max_abandoned_decisions": 32, "stop_on_failure": false},
  "seats": [{"player_id": "player_1", "bot": "random", "deterministic": true,
             "agent_metadata": { }}],
  "matches": [
    {"manifest_version": 1, "game_index": 0, "match_id": "arena_0", "seed": 8837,
     "outcome": "finished", "winners": ["player_2"],
     "log": "arena_20260906T142200Z_1234_0.jsonl",
     "actions": "arena_20260906T142200Z_1234_0.actions.jsonl",
     "seat_stats": {"player_1": {}}},
    {"manifest_version": 1, "game_index": 7, "match_id": "arena_7", "seed": 4412,
     "outcome": "forfeited", "winners": [],
     "ended_by": "player_3", "reason": "card_not_in_hand", "…": "…"},
    {"manifest_version": 1, "game_index": 9, "match_id": "arena_9", "seed": 991,
     "outcome": "failed", "winners": [],
     "ended_by": "player_2", "reason": "decision_timeout", "…": "…"}
  ]
}
```

Other pinned decisions:

- **Seat ids stay `player_{index + 1}`**, positional, so a seat means the same
  thing across every game in a run.
- **`run_id`** is `arena_{utc timestamp}_{seed}`. It is the only place a clock
  enters the arena, it names files and nothing else, and no engine or replay
  behaviour may read it. Its directory is created exclusively (D8).
- **The `observer` hook keeps its current contract** — authoritative `MatchState`,
  privileged test instrumentation only. It is not how bots see anything, and its
  docstring says so. Under concurrency it may be called from several threads, so
  document it as needing to be thread-safe; the volume tests that use it must
  satisfy that.
- **Winners are computed only for `FINISHED`.** Abandoned, forfeited and failed
  matches have no winner, and `winners` is empty rather than "whoever was ahead".

---

## Commit sequence

Each commit leaves the suite green and is independently complete. Run
`make check && make test` before each.

1. `refactor(engine): fold a viewer's stream once and keep it`
   — `ViewFolder`, `build_view` reduced to a wrapper over it, the explicit
   `explicit_counts` parameter with the legacy pre-scan kept for whole-log reads,
   and an equivalence test between incremental and whole-log folds. Engine-only,
   no caller changes.
2. `refactor(server): move persistence below the surfaces that write it`
   — `persistence/sink.py` and `server/stream.py` split out of `server/sink.py`,
   imports updated, the layering test extended to the whole table. Pure movement.
3. `perf(server): advance each viewer's fold instead of rebuilding it`
   — the store adopts `ViewFolder`. Separate from commit 1 so a live-server
   performance change is not buried in a refactor and a regression bisects to one
   of them.
4. `feat(arena): give bots the same view a player would read`
   — the log-keeping runner, `Bot.act(view) -> Action`, `RandomBot` ported,
   `BotObservation` and `observe_player` deleted. Classic only.
5. `feat(arena): tell a bot why its action was refused`
   — `Rejection`, the retry loop, `action_rejected` events on the seat's stream,
   the per-decision budget, forfeit and failure outcomes, `MatchOutcome`, the
   result types, and the parity test against the server's §9.4 error body. Fixes
   a live defect in classic runs.
6. `feat(arena): run negotiation matches under an explicit schedule`
   — rules and protocol as parameters, `arena/scheduling.py`, both schedulers,
   the classic-only guard on `sequential`, the row-choice bypass, the action
   limits and their precedence, abandonment. `random` learns to commit.
7. `feat(arena): record what produced a result`
   — the registry and `BotSpec`, `agent_metadata` on seats, `stats()` and its
   failure path, the `greedy` strategy, traces through `JsonlEventSink`, action
   records for every attempt with their outcomes and decision timing, the
   exclusive trace directory, the versioned manifest.
8. `feat(arena): play independent matches at the same time`
   — `concurrency`, bounded submission, per-match bot instances and schedulers,
   order-independent aggregation, the decision deadline and its daemon-thread
   execution, `max_abandoned_decisions`, `stop_on_failure`, and the equal-results
   test across concurrency levels. Separate from commit 7 so a determinism
   regression bisects cleanly.
9. `feat(analytics): summarise a match log into per-seat results`
   — `MatchSummary`, its optional manifest entry, `sixnimmt summarise`, and the
   arena/server parity test.
10. `feat(cli): configure and trace an arena run from the command line`
    — the new flags, outcome counts in the output, CLI tests.
11. `test(arena): pin the information contract and termination for arena play`
    — leak tests over arena seats, trace equivalence with a server log, replay
    after every accepted transition, the pathological-bot suite, negotiation
    volume (marked `arena_slow`).

---

## Tests

Follow `AGENTS.md`: one behaviour per test, descriptive names, `parametrize` over
cases of the same behaviour, fixtures reused rather than duplicated, and no
mocking of things that can be exercised directly.

*Folding (D1)*
- incremental and whole-log folds produce the same `MatchView` for every viewer
  after every appended batch, across a full match in both modes
- a legacy log with no `action_counted` still folds through `build_view`'s
  pre-scan branch unchanged
- a message-heavy play folds in time linear in its events, asserted as a bounded
  operation count rather than a wall-clock threshold

*Observation (D0)*
- an arena seat's view contains no other player's hand card, in any field, at
  every phase of a full match — compare card-bearing fields specifically, not
  every integer, or scores and counters produce false positives that bury the
  real ones
- under hidden card selection, no opponent card reaches a bot before
  `cards_revealed`
- a bot's view of a hidden direct message between two other seats is
  byte-identical to a run without it

*Rejections and failure (D4)*
- a bot playing one illegal action before every legal one completes a full match
  in both modes; every rejection appears on that seat's stream and nowhere else
- the `Rejection` handed to a retry carries the same code, message and
  `legal_actions` the HTTP server returns for the identical refusal — asserted
  against the server's own error body, parametrised over several error codes
- a bot that corrects itself from the message completes its match; one that
  ignores it forfeits after exactly `decision_rejection_limit` attempts, and the
  outcome names the seat and the last error code
- the budget resets on the next offer: a seat spending most of its budget on
  several consecutive offers does not forfeit
- an illegal `choose_row` during `AWAITING_ROW_CHOICE` gets its own budget
- a seat that raises on a known game index ends that match `failed`, naming seat,
  index, seed and exception, and the run completes the remaining games
- under `stop_on_failure` the same run stops and says where
- a bot returning a non-`Action` fails as a raise, naming the bot rather than
  surfacing an `AttributeError` from inside the engine
- the failure taxonomy: `BotSpec.build` raising on the first game fails the run
  and on a later game fails only that match; an unwritable trace directory and a
  raising scheduler fail the run; a raising `stats()` leaves the match untouched
  and is recorded in the manifest with its exception

*Scheduling and termination (D3, D5)*
- `round_robin` skips a committed seat until somebody uncommits, and resumes
  after the seat offered the last turn
- a never-committing seat under `round_robin` does not starve the others: every
  other seat is offered turns, and the play ends on the action limit
- `sequential` with negotiation is refused from `run_arena` and `run_match` as
  well as the CLI; an omitted scheduler resolves by mode
- during `AWAITING_ROW_CHOICE` only the awaited player is offered a turn, under
  both schedulers and both modes
- bots that message forever hit `play_action_limit` and abandon
- limits count attempts: a seat whose actions are half rejected reaches
  `play_action_limit` at the attempt count, not the acceptance count
- an accepted action that both ends the match and trips a limit yields
  `finished`, not `abandoned`
- precedence, constructed so the rejection budget and `play_action_limit` trip on
  the same attempt: the outcome is `forfeited` and names the seat
- an abandoned, forfeited or failed match contributes no win, tie or score
- one run exercises both `MatchProtocol.max_actions_per_play` and
  `play_action_limit`, producing different outcomes: `ACTION_BUDGET_EXHAUSTED`
  rejection versus abandonment

*Traces (D8)*
- an arena match's log replays to its live final state, field for field, after
  every accepted transition — per transition, not at the end, because hand-end
  banking zeroes the counters and an end-state comparison passes with counting
  entirely broken
- a fixture arena log and a fixture server log fold through the same `replay` and
  the same `MatchSummary`, and differ in no structural way
- action records exist for rejected attempts too, contiguously numbered in
  submission order, matching the server's numbering for the same sequence
- a timed-out decision produces a record with `type: None`, `outcome: "timeout"`,
  its `from_view`, and its timing fields
- each `from_view` matches the `view_id` of the view its bot was handed
- the manifest names every match, its seed and outcome, the forfeiting or failing
  seat with its reason, and per-seat stats
- a run whose trace directory already exists refuses to start
- a run without `--trace-dir` writes nothing and still produces the same
  aggregates

*Concurrency and deadlines (D9)*
- the same run at concurrency 1 and 8 with deterministic seats produces identical
  per-match logs ignoring timestamps, and identical aggregates
- aggregates do not depend on completion order, asserted with seats whose
  durations differ deliberately
- a bot instance is never entered by two matches
- at most `concurrency` matches are in flight, asserted by instrumenting
  concurrent entries rather than by timing
- a seat that blocks past `decision_timeout_seconds` ends its match `failed` with
  reason `decision_timeout` while other matches complete — and the match's own
  thread finalises it, which a test can prove by asserting the trace is written
  while the blocked call is still running
- a run exceeding `max_abandoned_decisions` stops and says so
- with no deadline set, `act` is called inline and no extra thread is created
- under `stop_on_failure`, matches already running are recorded and matches not
  yet started are not; `games_requested`, `games_started` and `games_completed`
  differ as the failure implies

*Determinism and volume*
- two runs of the same root seed with deterministic line-ups produce identical
  logs, ignoring timestamps
- 1 000 games at 2, 3, 5 and 10 players in negotiation mode with messaging bots:
  every row holds 1–5 cards and is strictly increasing, every card is in exactly
  one place, every hand empties, every match terminates, replay matches live
  state, information boundaries hold. Mark `arena_slow`.

*Registry, summary and CLI*
- an unknown bot name fails before the first game and lists the available names
- `MatchSummary` over an arena log and a server log agrees field for field on
  equivalent matches
- the same forfeited match summarised with and without its manifest entry reports
  `forfeited` with the seat, and `abandoned` with an explicit "no manifest" note
  — never `abandoned` while implying it knew
- latency comes from the decision stamps: an arena summary reports it, a server
  summary reports it as unavailable rather than zero
- a manifest entry with an unknown `manifest_version` is refused, not misread
- the new flags reach the run; output reports finished, abandoned, forfeited and
  failed separately

---

## Out of scope

- **An LLM-backed bot.** D7 lists what it needs and why it is separate.
- **The rest of phase 5's metric surface** beyond `MatchSummary` (D10).
- **Distributing a run** across processes or machines. `concurrency` is threads
  in one process, which suits I/O-bound seats and not CPU-bound ones nobody has.
- **Running arena matches through the HTTP server.** The arena is the in-process
  path; that is the point of it.
- **Bounded live event retention.** The arena holds a match's whole log in
  memory, which is fine for one match and is not a claim about the server.

### A known defect, deliberately not fixed here

`engine/events.py:82` defaults every event's `timestamp` to `datetime.now(UTC)`,
so the engine calls a clock, against the purity rule in §3. No transition reads
it, so nothing in this plan depends on it, but the fix — the persisting surface
assigns the timestamp and the engine leaves it unset — touches every event
construction site and wants its own change. Do not bundle it with this work.
