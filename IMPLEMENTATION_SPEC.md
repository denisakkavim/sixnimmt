# 6 nimmt! — Implementation Spec (v6)

**Component 1 of 4.** This document specifies one rules engine and the two surfaces that drive it: an **arena** that runs matches in-process for experiments, and an HTTP **server** that hosts matches for outside clients. The MCP server, UI, and agent harness are separate components, described here only where they constrain this one.

**Audience:** an engineer or Claude Code session implementing this from scratch. Everything needed should be here. Read §1 first — it is the shape of the whole thing, and everything after it is detail.

This version is intended to be implemented as written. The rules, the information model, and the API surface have been reviewed to convergence; §14 is the acceptance criteria.

---

## 0. What changed since v5

v5 described a game server that happened to contain an arena, introduced as a
volume test harness in phase 2 and never revisited. That was the wrong emphasis.
The arena is where experiments are run — line-ups of scripted, LLM and other
bots, played in volume, in classic and communication modes, with full traces kept
— and it is a first-class surface over the same engine as the server. This
revision says so, and pins the consequences.

- **§1 is rewritten.** Two surfaces, one engine, and the three "one X" rules that follow.
- **The arena has its own section, §16**: the bot interface, scheduling under communication, what happens when a bot acts illegally or fails, and what a run records. The old §16 is now §17.
- **Bots observe `MatchView`**, folded from their own audience-filtered stream — the same object an HTTP client reads. A separate in-process observation type is prohibited: a hand-copied subset of match state is the projection-and-blank pattern §9.2 forbids, and it passes every test written against the fields it happens to copy while leaking the first one somebody adds.
- **A rejected bot action is not a crash** (§16.4). `on_invalid_action = "reject"` is a rule about matches, not about transport, so it binds the arena exactly as it binds the server.
- **One trace format from both surfaces** (§11.3), which is why persistence stops living under `server/` (§13).
- **Analytics is no longer optional** (§12). Traces nobody derives anything from are just disk.
- **Determinism is qualified** (§11.2). A seed fixes the deal and the log replays; neither reproduces an LLM.
- **Views fold incrementally** (§10.2). Rebuilding a viewer's whole history per decision is quadratic, and the arena is where that stops being theoretical.
- **A bot is told why its action was refused** (§16.4). A rejection that only appends an event leaves the bot looking at a view identical to the one that produced the bad action, which is a retry loop that cannot converge. The arena hands back §9.4's payload, exactly as the server hands it to an HTTP caller.
- **LLM seats are assumed, not accommodated** (§16.6). They are slow, they fail, and they cost money, so matches run concurrently under a decision deadline, a failing seat is a recorded outcome rather than a dead run, and a bot may report opaque per-seat statistics into the manifest.
- **Decision latency is measured where it happens** (§11.1, §12). Deltas between action records measure submissions, not thinking, so the surface that calls a bot stamps when the decision started and ended. The server leaves those fields empty, because it cannot know.
- **Build order gains phase 7** (§15), and §14 gains arena acceptance criteria.

**A known defect this revision records rather than fixes.** §3 requires the
engine to call no clock, and `engine/events.py` defaults every event's
`timestamp` to `datetime.now(UTC)`. The rule is right and the implementation
violates it: stamping belongs to the surface that persists an event, not to the
engine that produced it. It is recorded here, and in §3, so it is a decision
rather than an oversight; fixing it touches every event construction site and
does not belong to the arena work.

---

## 0.1 What changed since v4

Phases 0–2 were implemented and reviewed against this document. Everything in
§2 came back clean: the head values, the dealing order, the §2.7 worked example
and the §2.8 invariants all reproduce exactly. The changes below are defects in
*this document* that the implementation exposed, plus one invariant the
implementation proved.

- **`row_choice_required` is `public`, not `player:<id>`** (§10.3). Keeping it
  private made §9.2's `awaiting` field impossible to derive from a viewer's own
  event stream, which would force the state-peeking §3 forbids. The event leaks
  nothing: `cards_revealed` has already published every card in the play.
- **`match_created` is two events** (§10.3). One event could not both carry
  `agent_metadata` to the log (§9.1) and withhold it from other players (§5.2).
- **Only the lowest card of a play can require a row choice** (§2.8). This is
  provable, and worth asserting.
- **§14's card-leak tests must be field-aware.** Scraping integers out of a
  serialised view flags scores and counters as if they were cards.
- **The public shadow of a re-selection must not vary with whether the card
  changed** (§14). The original wording admitted an implementation that leaked
  exactly that.
- **An open question is recorded** about a player's own action count under
  communication (§10.4), rather than left to be discovered mid-implementation.

## 0.2 What changed since v3

All five areas checked in the last review came back clean: the §2.7 arithmetic, the dealing order, the per-viewer cursor design, the `expected_view_version` semantics, and the absence of player-facing global counters. The remaining changes are implementation-boundary defects found in that review, plus four channels found alongside them.

- **Idempotency keys are scoped to the player** (§9.3). Keying only on `(match_id, action_id)` let one player collide with another's cached result. The key is now `(match_id, player_id, action_id)`, the cached result is bound to the action payload, and the cache is bounded **per player** — a shared bound would let heavy use by one player evict another's entries, which is itself an activity oracle.
- **`expected_view_version` is evaluated inside the serialization boundary** (§9.3, §9.5). Checking it before queueing leaves a TOCTOU window that makes the guard advisory rather than real.
- **Hidden events must not wake long polls or SSE subscribers** (§9.2). Waking a subscriber that then receives nothing is a timing oracle for events they cannot see.
- **`view_id` must not encode global state** (§9.2). If it were built from the global `seq`, it would reintroduce the leak the per-viewer cursor exists to close.
- **Rate limiting and quotas must be per player** (§9.5), for the same reason as the cache bound.
- **`MATCH_NOT_FOUND` and `NOT_AUTHORIZED` are indistinguishable** across matches (§9.4), so a token cannot be used to probe which matches exist.
- Direct messages emit **two separate events**, stated unambiguously (§10.3).
- New acceptance tests for all of the above (§14).

---

## 0.3 What changed in earlier passes

**Corrections**

- **The worked example in §2.6 was wrong, and the first correction of it was also wrong.** Card 23 is 1 head, not 2 (v2's error). Card 44 is a multiple of 11 and therefore **5 heads**, not 1 (the reviewer's error). The correct total is **12**, not 9 and not 8. Every card in the example now carries its head value inline so this cannot recur.
- **Seeds were leaking.** v2 put the match seed and hand seed in events with audience `all`, contradicting the information contract. A player holding the match seed can reproduce the shuffle and read every hand. Seeds are now admin-only.
- **Global sequence numbers were leaking.** A player could infer hidden activity from gaps in event sequence numbers and from bumps in the global state version. Players now see a gap-free, per-viewer cursor (§10.2).

**New precision**

- Exact dealing order, and `hand_number` is 1-based (§2.2).
- Board invariants stated and testable (§2.7).
- Vocabulary fixed: play, hand, match (§2.3).
- Linearization point of the final commit stated in terms of serialization order (§7.2).
- Explicit audience classes replacing the ambiguous `"all"` (§10.1).
- Known-answer seed vectors for the shuffle (§11.2).
- Resource lifecycle for abandoned matches (§11.4).
- `from_view` defined as an audit reference, not a concurrency token (§9.3).
- `legal_actions` defined as advisory; the server is always authoritative (§9.2).
- Pathological-agent criteria distinguish internal deadlock from legitimate game non-progress (§14).

---

## 1. Purpose and scope

A rules engine for 6 nimmt! played between 2–10 players, driven through two
surfaces.

- **The server** hosts matches over HTTP for callers outside the process: LLM agents driven by a harness, or people through a UI. It authenticates, serialises concurrent actions, and hands every caller a view filtered to what they are allowed to know. §9 specifies it.
- **The arena** runs matches in-process, with no HTTP and no network, so a line-up of bots can be played thousands of times and studied. A bot may be a scripted strategy, an LLM, or anything else that can answer "what do you do now". §16 specifies it.

The engine is the **single source of truth**. It owns the deck, the shuffle, the
hands, the rules, and the turn structure. Neither surface owns any of it. A
player — human, agent or bot — cannot see hidden information, cannot make an
illegal move, and cannot advance the game except through the actions the engine
offers.

Three rules follow from having two surfaces, and they are the spine of this
document. Each is a thing that will be tempting to violate for local
convenience, and each is specified so that it cannot be.

1. **One information model.** A bot in the arena receives a `MatchView` folded from its own audience-filtered event stream — the same object, built by the same code, that an HTTP client gets from `GET /state`. There is no second, more convenient observation type for in-process play. Convenience is exactly how a leak gets in (§5, §9.2, §16.2).
2. **One trace format.** Every match, from either surface, writes the same JSONL event log and the same action records (§11.3). Replay and analytics read one format and cannot tell which surface produced a log.
3. **One place an experiment is configured.** `MatchProtocol` says what kind of match this is — classic or communication, budgets, information policy, anonymisation. The arena varies it, the server exposes it at match creation, and neither invents a setting of its own (§6, §16.1). A knob the server could not also express would produce results that say nothing about real play.

Two modes, one engine:

- **Classic.** Every player secretly picks a card; picking commits it. When all have committed, cards are revealed and resolved. This is the board game, and this is what the first working version must do correctly.
- **Communication.** Before commitment, players exchange messages and may change their card freely. The play proceeds only once every player has committed.

Build classic first, on the communication-shaped state machine. Classic mode is communication mode with messaging switched off and "select implies commit" — a config flag and roughly twenty lines. Retrofitting revocable selections onto a select-and-reveal loop means rewriting the play lifecycle. Defer the *features* of communication; do not defer the *shape*.

### Out of scope

- **Any LLM call by the engine or the server.** Neither talks to a model, and neither has a notion of one. An arena bot may call whatever it likes; that is the bot implementation's boundary, and the arena knows only that something answered (§16.4).
- MCP. That server is a separate process and a client of this API.
- Real authentication. Bearer tokens minted at match creation are enough.
- Rendering. The UI is a separate client.
- Analytics beyond a derivation module over the event log (§12).
- Distributed or multi-process arenas, and running the arena's matches through the HTTP server. One process, in-process, is the whole point of it (§16).

---

## 2. The rules

Implement these exactly. Treat this section as authoritative rather than relying on memory of the board game.

### 2.1 The deck

104 cards, numbered 1 to 104. Each carries a number of **bull heads** (penalty points). Apply these in order; first match wins.

1. Card 55 → **7**
2. Multiple of 11 (11, 22, 33, 44, 66, 77, 88, 99) → **5**
3. Multiple of 10 (10, 20, … 100) → **3**
4. Multiple of 5 (5, 15, 25, …) → **2**
5. Everything else → **1**

Totals, which the tests must assert:

| Group | Cards | Heads each | Subtotal |
|---|---|---|---|
| Card 55 | 1 | 7 | 7 |
| Other multiples of 11 | 8 | 5 | 40 |
| Multiples of 10 | 10 | 3 | 30 |
| Other multiples of 5 | 9 | 2 | 18 |
| Remainder | 76 | 1 | 76 |
| **Total** | **104** | | **171** |

Note the ordering trap: rule 2 fires before rules 3 and 4, so 44, 66, 88 and 99 are 5 heads even though 44 and 88 look unremarkable and 66 is also even. Two independent reviewers of this spec have already scored 44 as 1 head. Write the value table as a literal lookup in the tests, not as a reimplementation of the rules, so the tests cannot repeat the bug they exist to catch.

### 2.2 Setting up a hand

Hands are numbered from 1. The first hand of a match is `hand_number = 1`.

1. Build the ordered list `[1, 2, … 104]`.
2. Shuffle it with that hand's seeded generator (§11.2). Call the result the **deck**, position 0 first.
3. Deal to players **round-robin in seating order, one card at a time, for 10 passes**. Seating order is the order of the `players` array at match creation. So with players A, B, C the deck's first three cards go to A, B, C, the next three to A, B, C, and so on.
4. The next **4** cards, in deck order, become the starting cards of rows 0, 1, 2 and 3 respectively.
5. Any remaining cards are the **undealt remainder**. They are set aside, never used, and never revealed to anyone — including omniscient observers, so that leaked knowledge can never explain a result.

With 10 players this consumes all 104 cards and the remainder is empty. With 2 players, 80 cards go unused.

A player's hand may be held sorted for presentation, but the dealing order above is what the seed determines and what the tests assert.

### 2.3 Vocabulary

Fixed terms, used consistently and never interchangeably:

- A **play** is one round: every player commits one card, all are revealed, all are resolved.
- A **hand** is 10 plays. Every player contributes one card per play, so all hands empty simultaneously.
- A **match** is one or more hands, ending per §2.5.

Do not use "round" anywhere.

### 2.4 A play

1. Every player chooses one card from their hand and commits it (§7).
2. All committed cards are revealed at once.
3. Cards are resolved **one at a time, in ascending order of card number** — never seating order, never simultaneously. Order matters enormously and is the most common thing implementations get wrong.

### 2.5 Placing one card

For the card currently being resolved:

- Look at the **last card** (rightmost, highest) of each row.
- Eligible rows are those whose last card is **strictly lower** than the played card.
- **If any row is eligible:** the card goes on the eligible row whose last card is **highest** — the row it fits most tightly. Formally, the chosen row satisfies `chosen_row.last == max(r.last for r in rows if r.last < played_card)`.
  - If that row already held 5 cards, the played card would be its sixth. Instead the player **takes all 5 cards** into their penalty pile, the row is emptied, and the played card becomes its only card.
  - Otherwise the card is appended.
- **If no row is eligible** (the card is lower than every row's last card): the player **must take one entire row of their choice**, of any length. Those cards go to their penalty pile, the row is emptied, and the played card becomes its only card.

Then move to the next-lowest card in the play. Each card sees the board as left by all lower cards in the same play.

### 2.6 Scoring and ending

- A player's hand score is the total bull heads in their penalty pile.
- **Lower is better.**
- Scores accumulate across hands.
- **The match ends at the end of the first hand in which any player's cumulative score reaches 66 or more.** Check only between hands. Checking mid-hand would let the resolution order inside a single play decide the match, which is arbitrary.
- The winner is the player with the lowest cumulative score. Report ties as ties; do not break them.

### 2.7 Worked example

Verify against this exactly, including the intermediate boards.

```
Rows before the play:
  row 0: [7]
  row 1: [23, 25, 30, 41, 44]      <- already 5 cards
  row 2: [52]
  row 3: [88]

Cards played:  Alice 45,  Bob 3,  Cara 53,  Dan 46
```

Resolved ascending — 3, 45, 46, 53:

**3 (Bob).** Row ends 7, 44, 52, 88. None is below 3, so no row is eligible: Bob must choose a row. Say row 0.

- Bob captures `[7]` → 7 is 1 head. **Bob: 1.**
- Row 0 becomes `[3]`.
- Board: `[3] / [23,25,30,41,44] / [52] / [88]`

**45 (Alice).** Row ends 3, 44, 52, 88. Eligible: rows 0 and 1. Highest end is row 1 (44). Row 1 holds 5 cards, so Alice captures it.

| Card | Rule | Heads |
|---|---|---|
| 23 | none | 1 |
| 25 | multiple of 5 | 2 |
| 30 | multiple of 10 | 3 |
| 41 | none | 1 |
| **44** | **multiple of 11** | **5** |
| | **Total** | **12** |

- **Alice: 12.**
- Row 1 becomes `[45]`.
- Board: `[3] / [45] / [52] / [88]`

**46 (Dan).** Row ends 3, 45, 52, 88. Eligible: rows 0 and 1. Highest end is row 1 (45), which holds 1 card, so 46 is appended. **Dan: 0.**

- Board: `[3] / [45,46] / [52] / [88]`

**53 (Cara).** Row ends 3, 46, 52, 88. Eligible: rows 0, 1 and 2. Highest end is row 2 (52). Appended. **Cara: 0.**

- Board: `[3] / [45,46] / [52,53] / [88]`

```
Rows after:  row 0 [3]   row 1 [45, 46]   row 2 [52, 53]   row 3 [88]
Penalties:   Bob 1,  Alice 12,  Cara 0,  Dan 0
```

Alice was punished by a row Bob's card never touched, and Bob's choice changed which rows were eligible for Alice. That coupling is why resolution must be strictly sequential.

### 2.8 Invariants

These hold after every placement and are worth asserting in a debug mode and in property tests:

- Every row holds between 1 and 5 cards.
- Every row's cards are strictly increasing left to right.
- Every card from 1 to 104 exists in exactly one place: a player's hand, a row, a penalty pile, or the undealt remainder. Never two, never none.
- Total bull heads across all penalty piles plus all cards still in play equals 171.
- When at least one row is eligible, the chosen row's end card equals the maximum end card strictly below the played card.
- **At most one card in a play can require a row choice, and it is always the lowest.** Once the lowest card resolves, some row ends exactly on it, so every higher card in the same play is guaranteed an eligible row. An implementation that produces a second row choice within one play has broken the rule that an emptied row becomes `[played_card]`.

---

## 3. Design principles

**The engine is a pure function.** No I/O, no network, no clock, no ambient randomness. Its whole surface is:

```
transition(state, action) -> (new_state, [events])   |   Rejection(code, message)
```

**Surfaces drive; they never decide.** A surface authenticates or schedules, builds an action, calls the engine, persists the events it gets back, and hands over a filtered view. The HTTP server does this across a socket; the arena does it in a loop. Neither may contain a rule, a legality check, or a projection of its own. If a surface can answer a question about the game that the engine could not, that is a defect and not a shortcut. Everything worth testing is therefore testable without a server, and an experiment runs at full speed in one process.

**There is no time in the engine.** No deadlines, no timers, no `now()`. A match progresses only when a player acts. Liveness is the harness's job: it decides how long to wait for a model and abandons matches that stall. Wall-clock timestamps are recorded on events for analysis but never influence a transition.

> **Known defect.** `engine/events.py` currently defaults `timestamp` to
> `datetime.now(UTC)`, so the engine does call a clock. The stamp is only ever
> read by analytics and never by a transition, so no rule depends on it, but the
> rule above is the one that should hold: the surface that persists an event
> assigns its timestamp, and the engine leaves it unset. Fixing this touches
> every construction site and is deliberately not bundled with other work.

**Everything that happens is an event.** State is the fold of the event log. The log drives the UI, replays, and behavioural analysis. Never change state without emitting a matching event.

**Every event carries an audience.** A viewer's state is built by folding only the events they may see. That is the sole mechanism preventing information leaks — not per-endpoint filtering, which is where leaks come from.

**Assume clients are hostile and incompetent.** LLM agents will send malformed JSON, cards they do not hold, actions in the wrong phase, and the same request three times. None of it may crash the server or corrupt a match.

---

## 4. Settled configuration

| Key | Default | Meaning |
|---|---|---|
| `end_condition` | `"target_score"` | Play until someone reaches the target. |
| `target_score` | `66` | The real game's threshold. |
| `hands` | `null` | Used only when `end_condition = "fixed_hands"`. |
| `communication_enabled` | `false` initially | Turns on messaging and revocable selections. |
| `information_policy.card_selection` | `"hidden"` | Nobody sees another's card before reveal. |
| `information_policy.private_message_existence` | `"visible"` | Others see *that* Alice messaged Bob, never the text. |
| `allow_direct_messages` | `true` | |
| `max_actions_per_play` | `null` | Unlimited. The mechanism exists; the bound is off. |
| `max_message_length` | `2000` | Characters. |
| `on_invalid_action` | `"reject"` | Rejected, counted, retried; never costs the turn. |
| `anonymise_display_names` | `false` | When true, players see opponents as "Player 2" etc. |

Note on the action budget: because it is `null` by default and there is no clock, **the server cannot guarantee a play terminates**. Two accommodating models can communicate forever. This is a deliberate choice; the harness must impose its own limit and abandon stalled matches. Implement the counting and the cap check anyway so switching it on is a config change, not a redesign. The state view always reports actions taken, and reports remaining as `null` when unlimited.

---

## 5. The information contract

This is a security specification. Every clause needs a test.

### 5.1 A player may know

- Their own hand.
- Every row's full contents.
- Every player's penalty pile and score, current and past hands.
- Cards revealed in completed plays of the current hand.
- Which players have a selection, and which have committed — the fact, never the card.
- Table-wide messages.
- Direct messages addressed to them, or sent by them.
- That a direct message occurred between two other players, when `private_message_existence = "visible"` — parties only, never content.
- Their own action count and remaining budget.
- The match configuration and rules.

### 5.2 A player may never know

- Another player's hand.
- Another player's selected card before reveal, under `card_selection = "hidden"`.
- The content of a direct message not addressed to them.
- That a direct message occurred at all, under `private_message_existence = "hidden"`.
- Any card in the undealt remainder.
- The match seed or any hand seed, and any future random state.
- Another player's agent metadata, unless the operator has made it public.
- Another player's rejected actions.
- Another player's action count, unless a budget is in force, in which case remaining budget is public because it constrains what they can still do.

### 5.3 No inference through metadata

> A player must not be able to infer hidden events, actions, messages, cards, or state transitions from sequence numbers, version numbers, cursor gaps, event counts, identifiers, response shapes, error payloads, or any other metadata.

This is the rule that v2 violated in two places, and it is easy to violate again. Two concrete channels it closes:

- Under `private_message_existence = "hidden"`, a bump in a globally-numbered cursor would tell Alice that *something* happened that she cannot see, defeating the setting entirely.
- `action_rejected` is private to the offending player. Under a global sequence, twenty rejected actions from Bob would advance Alice's cursor twenty times, telling her Bob is flailing. This leaks under the **default** configuration, not just an exotic one.

§10.2 specifies the mechanism that satisfies this rule.

A corollary the implementation must maintain: **any state change that alters what a viewer may legally do must produce at least one event visible to that viewer.** Otherwise a gap-free per-viewer cursor would conceal a change that actually matters to them. Under the current event set this holds — every commitment transition has a public event — but check it whenever a new event type is added.

### 5.4 Roles

| Role | Sees |
|---|---|
| `player` | Exactly §5.1. |
| `public_spectator` | What an onlooker at the table sees: rows, revealed cards, penalty piles, scores, commitment status, table messages, and the fact of a direct message when its existence is visible. No hands, no direct message content, no seeds. |
| `omniscient_observer` | Every `public` and `player:<id>` event, so all hands and all direct message content. **Not `admin` events**, and so not seeds (§10.1), and never the undealt remainder. What the development UI uses. **Never issue this to an agent.** |
| `admin` | Everything an omniscient observer sees, plus `admin` events — seeds and agent metadata — and match creation and deletion. |

`public_spectator` exists because games will eventually be shown to humans who must not be handed an omniscient view by accident. The audience model already supports it, so it costs almost nothing now and is awkward to retrofit.

### 5.5 Errors obey the same boundary

> An error response may reveal only information the caller could already obtain through its normal view — including any version, cursor, or sequence number it carries.

Telling a player their own hand when they select a card they do not hold is fine. Anything that would let an agent probe for hidden state by submitting speculative actions is not. An error must never mention another player's card, hand, or private message content, and must carry the caller's own `view_version`, never a global one.

---

## 6. Game rules versus match protocol

Keep these as separate objects. Game rules describe 6 nimmt!. Match protocol describes an experiment. Mixing them means every new evaluation idea edits the rules engine.

```python
GameRules(
    min_players=2,
    max_players=10,
    cards_per_hand=10,
    row_count=4,
    row_capacity=5,
    deck_size=104,
    target_score=66,
)

MatchProtocol(
    end_condition="target_score",  # or "fixed_hands"
    hands=None,
    communication_enabled=False,
    information_policy=InformationPolicy(...),
    max_actions_per_play=None,
    on_invalid_action="reject",
    anonymise_display_names=False,
)
```

`target_score` sits in `GameRules` because 66 is the published rule, not an experimental parameter. `end_condition = "fixed_hands"` is a protocol-level override for controlled comparisons where variable match length would be inconvenient.

---

## 7. Match and play state

### 7.1 Phases

```
SETUP -> SELECTING -> RESOLVING -> (next play | next hand | FINISHED)
                          ^   |
                          |   v
                    AWAITING_ROW_CHOICE
```

### 7.2 Commitment

Each player holds a **selection** (a card or nothing) and a **committed** flag.

The vocabulary is `select` / `commit` / `uncommit`. State the meaning plainly in the code and the docs:

> Committing means "I will not change this card unless the protocol lets me uncommit." The server checks whether every player has committed. It never decides whether players *agree*. A player may hate the plan the table has settled on and commit anyway, and messages are never parsed for consent.

Rules:

- `select_card(card)` sets the selection and **clears the caller's own committed flag**. You cannot be committed to a card you just changed. It clears nobody else's.
- `commit()` requires a selection.
- `uncommit()` is legal only while at least one other player is uncommitted.

**Linearization.** Actions are serialized per match (§9.5). Once the serialized transition that accepts the final required `commit` completes, the play is irrevocably committed and the phase is RESOLVING. No subsequent action — `uncommit`, `select_card`, or anything else — can modify the committed selections. What matters is the server's serialization order, not packet arrival time or client timestamps.

This makes committing a real commitment: you cannot commit to bait a reaction and then withdraw, unless your `uncommit` is serialized before the other player's final `commit`.

In classic mode (`communication_enabled = false`), `select_card` commits implicitly, `uncommit` is not offered, and messaging is disabled. Same code path, no window in which anyone can react.

### 7.3 Messages

A message has a sender, a visibility, and a body.

- `visibility = "table"` — all players.
- `visibility = "direct"` with a `to` player — sender and recipient see the content. Whether anyone else learns a message occurred is governed by `private_message_existence`.

Plain text, capped at `max_message_length`. The server does not interpret, validate, or moderate. A player claiming to hold a low card may be lying; that is the point.

Messages are legal only during SELECTING, keeping the communication window well defined.

When `private_message_existence = "visible"`, emit two events: a content-bearing one addressed to sender and recipient, and a content-free `private_message_occurred` addressed publicly, naming only the two parties. When hidden, emit only the first — and note that §5.3 is what stops the omission from being detectable.

---

## 8. Resolution as an explicit state machine

Do not write a loop that conceptually pauses. Make resolution a value in the match state:

```python
ResolutionState(
    ordered_cards=[(3, "bob"), (45, "alice"), (46, "dan"), (53, "cara")],
    next_index=2,
    awaiting_player=None,  # or "bob" while a row choice is outstanding
)
```

The engine exposes a deterministic `advance_resolution(state)` that either places the next card, or transitions to AWAITING_ROW_CHOICE and stops. `choose_row` fills in the pending choice and resumes.

This makes pause, resume, replay, and debugging straightforward, and it means a match can be serialised mid-resolution and restored.

While `awaiting_player` is set, only that player may act, and only `choose_row`. Every other action is rejected with `NOT_YOUR_TURN`, naming who the game is waiting for.

---

## 9. HTTP API

JSON throughout. `Authorization: Bearer <token>`. Tokens are minted at match creation and returned exactly once.

### 9.1 Match management

**`POST /matches`** *(admin)*

```json
{
  "players": [
    {
      "id": "alice",
      "display_name": "Player 1",
      "agent_metadata": {
        "provider": "anthropic", "model": "claude-opus-5",
        "harness_version": "0.3.1", "strategy_id": "baseline",
        "temperature": 0.7, "system_prompt_id": "sp_004"
      }
    }
  ],
  "seed": 12345,
  "rules": { },
  "protocol": { }
}
```

`agent_metadata` is opaque to the server: never parsed, never shown to other players, always written to the log. It exists so that in six months you can tell exactly which model and harness produced a result. Keep display names neutral, or set `anonymise_display_names`, so opponents cannot condition their play on who they are facing unless you want them to.

Returns the match ID, the fully resolved rules and protocol with defaults filled in, the seed actually used, a player token each, a public spectator token, and an omniscient token. Seating order is the array order and determines the deal (§2.2). If `seed` is omitted, generate and return one so any match is reproducible. **The seed is returned in this admin response and nowhere else** (§10.3).

**`POST /matches/{id}/start`** — deals the first hand, opens the first play.
**`GET /matches`** — list with status and scores.
**`DELETE /matches/{id}`** — abandon (§11.4). This is how the harness disposes of a stalled communication.

### 9.2 Reading state

**`GET /matches/{id}/state`** returns the caller's view.

> **Build this response by folding the caller's visible events, not by projecting `MatchState` and blanking private fields.** This is the §3 rule restated at the point it is most often broken. The shape below looks like a projection of match state, and copying fields across and nulling the private ones passes every obvious test while leaving each future field one oversight away from a leak. Fold the filtered stream and a fact the caller was never sent has no way to reach them.


```json
{
  "match_id": "m_01",
  "view_version": 87,
  "view_id": "v_9f2c",
  "status": "in_progress",
  "phase": "selecting",
  "hand_number": 2,
  "play_number": 5,
  "you": {
    "player_id": "alice",
    "hand": [4, 19, 62, 77, 91, 103],
    "selection": 62,
    "committed": false,
    "penalty_cards": [23, 25, 30, 41, 44],
    "score_this_hand": 12,
    "total_score": 27,
    "actions_taken_this_play": 6,
    "actions_remaining_this_play": null
  },
  "rows": [
    {"index": 0, "cards": [3]},
    {"index": 1, "cards": [45, 46]},
    {"index": 2, "cards": [52, 53]},
    {"index": 3, "cards": [88]}
  ],
  "players": [
    {"player_id": "bob", "display_name": "Player 2", "cards_in_hand": 6,
     "has_selection": true, "committed": true, "selection": null,
     "penalty_cards": [11, 30], "score_this_hand": 8, "total_score": 31}
  ],
  "revealed_this_hand": [[7, 12, 40, 91], [2, 33, 58, 77]],
  "awaiting": null,
  "legal_actions": ["select_card", "commit"],
  "target_score": 66
}
```

- `you.hand` appears only for the caller. **No other player's hand card appears anywhere in a player view, ever.**
- Another player's `selection` is `null` under the hidden policy. `has_selection` and `committed` are always public — knowing *that* someone has committed is legal information; knowing *what* is not.
- Penalty piles are public. Agents may legitimately track which cards have left play.
- `view_version` is the caller's own gap-free cursor (§10.2), never a global counter. No global `version` field is exposed to players.
- `view_id` is an opaque identifier for this exact projection (§9.3). It must be genuinely opaque: a random token, or a value derived only from the caller's own cursor. **Never derive it from the global `seq`, a global version, or a timestamp** — a `view_id` that encodes global state reintroduces exactly the leak the per-viewer cursor exists to close, in a field an implementer is likely to treat as cosmetic.
- `actions_remaining_this_play` is `null` when no budget is in force.
- `awaiting` names the player the game is blocked on, or `null`.
- `legal_actions` is **advisory presentation information**. MCP and UI clients may use it to hide unavailable actions, but the server independently validates every action and remains authoritative. The MCP layer must never become part of the rules engine.

**`GET /matches/{id}/events?since={view_version}&limit={m}`** — events visible to the caller, numbered by the caller's own cursor, `since` exclusive. Contiguous by construction: the caller sees 1, 2, 3, … with no gaps, regardless of what else happened in the match.

**`GET /matches/{id}/wait?since={view_version}&timeout={s}`** — long poll, returning as soon as there is a new visible event or the caller has legal actions, else on timeout (default 30s, max 60s). This is a transport convenience with no effect on game state; it is not a deadline. Agents should use it instead of tight polling.

**`GET /matches/{id}/stream`** — Server-Sent Events, filtered to the caller's audience and numbered by their cursor. Honour `Last-Event-ID` on reconnect.

**Wakeups are part of the information contract.** Both endpoints must wake only when an event enters *that subscriber's visible stream*:

> Long-poll and SSE subscribers are notified only when an event enters that subscriber's visible event stream. An event invisible to a subscriber must neither wake the request nor advance its cursor.

The failure this prevents: Alice opens `/wait`, Bob sends a hidden direct message, the server wakes every waiter because "an event was appended", and Alice's call returns early with nothing new. The empty early return tells her something happened. Implement the wake condition on visible-stream advancement, not on appends to the match log; for SSE this falls out naturally if fan-out happens after audience filtering rather than before.

Test the deterministic server property — a hidden event produces no wake notification — rather than trying to assert timing equality, which network scheduling makes noisy.

### 9.3 Actions

The canonical endpoint is generic, which maps cleanly onto a typed action model and onto MCP tools:

**`POST /matches/{id}/actions`**

```json
{
  "action_id": "b3f1-...",
  "type": "select_card",
  "card": 62,
  "from_view": "v_9f2c",
  "expected_view_version": 87
}
```

Action types: `select_card`, `commit`, `uncommit`, `send_message`, `choose_row`.

Keep thin wrappers for readability and direct human use: `POST /matches/{id}/select`, `/commit`, `/uncommit`, `/message`, `/choose_row`. They construct the same action object.

- `action_id` is a client UUID for idempotency. **The key is `(match_id, player_id, action_id)`, where `player_id` comes from the authenticated token, never from the request body.** Keying on `(match_id, action_id)` alone would let one player collide with another's cached result — an unnecessary cross-principal boundary, and an oracle for whether another player used a given id.
  - Same key, same action payload → return the cached result without reapplying.
  - Same key, different payload → reject with `IDEMPOTENCY_KEY_REUSED`. Never silently return the old result for a different request. Compare the action type and its parameters (`card`, `row_index`, message fields); ignore `from_view` and `expected_view_version`, which are metadata a legitimate retry may vary.
  - Retain roughly the last 1000 entries **per player**, not per match. A shared bound would let a player who floods the server evict another player's entries, and the resulting change in dedup behaviour is itself an activity oracle.
- `from_view` is **an audit reference only**. It is an opaque identifier for a specific caller-visible projection generated by the server, recording which observation the client says it acted upon. It is not a concurrency token, it is never validated for freshness, and a stale `from_view` never causes rejection. Combined with the audience-filtered event log it answers "what did this agent know when it acted" without a parallel observation store. MCP and harness implementations must not assume otherwise.
- `expected_view_version` is optional optimistic concurrency, expressed in the caller's own cursor. If supplied and the caller's cursor has advanced, reject with `VERSION_CONFLICT`. Using the caller's cursor rather than a global version is both leak-free and semantically sharper: it asks "has anything I can see changed since I looked?", which is the question the client actually cares about. Agents need not supply it; the harness may attach it.

  **The check happens inside the per-match serialization boundary**, at the same linearization point that accepts or rejects the action (§9.5). Evaluating it before queueing leaves a window in which another player's visible action is applied between the check and the transition, which makes the guard advisory rather than real.

Every action returns the caller's updated state view, so one round trip per move.

### 9.4 Errors

```json
{
  "error": {
    "code": "CARD_NOT_IN_HAND",
    "message": "You tried to select card 47, which is not in your hand. Your hand is: 4, 19, 62, 77, 91, 103.",
    "legal_actions": ["select_card", "commit"],
    "view_version": 87
  }
}
```

Minimum codes: `MATCH_NOT_FOUND`, `NOT_AUTHORIZED`, `MATCH_NOT_STARTED`, `MATCH_ALREADY_STARTED`, `MATCH_FINISHED`, `MATCH_ABANDONED`, `WRONG_PHASE`, `NOT_YOUR_TURN`, `CARD_NOT_IN_HAND`, `NO_SELECTION_TO_COMMIT`, `CANNOT_UNCOMMIT_WHEN_ALL_COMMITTED`, `COMMUNICATION_DISABLED`, `INVALID_ROW_INDEX`, `ROW_ALREADY_CHOSEN`, `ACTION_BUDGET_EXHAUSTED`, `MESSAGE_TOO_LONG`, `DIRECT_MESSAGES_DISABLED`, `RECIPIENT_NOT_FOUND`, `VERSION_CONFLICT`, `IDEMPOTENCY_KEY_REUSED`, `MALFORMED_REQUEST`, `UNKNOWN_ACTION_TYPE`.

`MATCH_NOT_FOUND` and `NOT_AUTHORIZED` must be **indistinguishable** when a token is used against a match it has no rights to: same code, same shape, same latency class. Otherwise a token becomes a probe for which match IDs exist. Return `MATCH_NOT_FOUND` for both.

Always include current `legal_actions`. It is the single most useful thing to hand a confused agent. Write messages that state the problem and the correct alternative: "the play is resolving and the game is waiting for bob to choose a row" beats "invalid action". Respect §5.5 in every message and in the `view_version` carried.

A rejected action changes no game state. It emits an `action_rejected` event visible only to the offending player, which advances only that player's cursor. Rejections are counted per player and available to analytics. Never swallow them silently.

### 9.5 Concurrency

All players act simultaneously, so concurrent writes are normal.

- Serialise all state changes per match behind one lock or a per-match queue. Two `commit` calls arriving together must not both observe "someone else is still uncommitted".
- The serialization order is the linearization point for §7.2.

Everything that could race happens inside the boundary, in this order:

1. Acquire the per-match serialization boundary.
2. Look up the idempotency key `(match_id, player_id, action_id)`; on a payload-matching hit, return the cached result and stop.
3. Assign `server_action_seq`.
4. Evaluate `expected_view_version` against the caller's authoritative current cursor.
5. Apply the transition, or reject.
6. Append events and advance each affected viewer's cursor.
7. Cache the result under the idempotency key.
8. Release.

Nothing in steps 2 and 4 may be hoisted outside the boundary as an optimisation; both become meaningless if they are.

**Rate limits and quotas, if added, must be per player.** A per-match limit lets one player's traffic throttle another, and the resulting change in response behaviour is an activity oracle in exactly the way §5.3 forbids.

---

## 10. Events

### 10.1 Audience classes

`audience` is one of exactly these, never the ambiguous string `"all"`:

| Class | Delivered to |
|---|---|
| `public` | Every player, public spectators, omniscient observers, admin. |
| `player:<id>` | That one player, plus omniscient observers and admin. |
| `admin` | Admin only. Not omniscient observers, not the UI. |

An engineer must not be able to read an audience value as "everyone gets this" and thereby expose privileged data. If an event has any recipient-specific content, it is `player:<id>` and there is one event per recipient.

### 10.2 Global sequence versus viewer cursor

Two distinct numbering schemes, and conflating them is the leak that v2 shipped:

- **`seq`** — a global, contiguous, authoritative sequence over every event in the match. It appears in the log, drives replay, and is visible only to admin and omniscient observers.
- **`view_version`** — a per-viewer cursor over that viewer's *filtered* event subsequence, numbered 1, 2, 3 … with **no gaps**. This is what players see in `/state`, `/events`, `/wait`, `/stream` and error payloads.

Implement the cursor as an index into the viewer's filtered stream, so gap-freeness is structural rather than something a future change can accidentally break. A hidden event simply does not appear in that viewer's stream and therefore does not advance their cursor.

**Fold incrementally for a live match.** A viewer's view is the fold of their
filtered stream, but rebuilding it from event one on every read makes the cost of
a match quadratic in its own length — and communication, where a play may carry
hundreds of messages, is where that stops being theoretical. Keep a live folder
per viewer, advance it with the events just appended, and project when asked.
Folding a whole log from the start remains the operation replay and analytics
use, and the two must agree: an incrementally folded view and a view rebuilt from
the same events are the same view, and that is a test, not a hope.

Do not expose any global mutation version to players, in any endpoint or error.

### 10.3 Event shape and catalogue

```json
{
  "seq": 412,
  "match_id": "m_01",
  "type": "player_committed",
  "server_action_seq": 207,
  "timestamp": "2026-09-04T10:31:02.145Z",
  "hand": 2,
  "play": 5,
  "audience": "public",
  "data": {"player_id": "bob"}
}
```

| Type | Audience | Notes |
|---|---|---|
| `match_created` | public | rules, protocol, players as id and display name — **no seed, no agent metadata** |
| `match_created` | admin | the same, plus each player's `agent_metadata` |
| `match_seed_assigned` | admin | the match seed |
| `match_started` | public | |
| `hand_started` | public | hand number — **no seed** |
| `hand_seed_assigned` | admin | the derived hand seed |
| `cards_dealt` | `player:<id>` | one event per player, their hand only |
| `rows_initialised` | public | the four starting cards |
| `play_started` | public | play number |
| `selection_made` | `player:<id>` under hidden policy; public under public policy | the card |
| `selection_registered` | public | that a selection exists, no card |
| `selection_cleared` | public | on change or uncommit |
| `player_committed` | public | |
| `player_uncommitted` | public | |
| `message_sent` (table) | public | content |
| `message_sent` (direct) | two events: one `player:<sender>`, one `player:<recipient>` | each carries the content |
| `private_message_occurred` | public | parties only, no content; emitted only when existence is visible |
| `play_committed` | public | the moment unanimity was reached |
| `cards_revealed` | public | every player's card |
| `card_placed` | public | card, row index, resulting row |
| `row_taken` | public | player, row, captured cards, heads, reason (`sixth_card` / `too_low`) |
| `row_choice_required` | public | the player and the card. Public because `cards_revealed` already published the card, and because `awaiting` (§9.2) must be derivable from every viewer's stream |
| `row_choice_made` | public | |
| `play_ended` | public | per-player penalty this play |
| `hand_ended` | public | hand scores and running totals |
| `match_ended` | public | final scores, winner or winners |
| `match_abandoned` | public | |
| `action_rejected` | `player:<id>` | code and description |

Three rules that are easy to violate and expensive to fix:

- Never put hidden data in a `public` event assuming the API layer will strip it. The audience field is the only filter.
- Seeds appear only in `admin` events and the admin creation response. A player holding the match seed can reproduce every shuffle and read every hand; this is a total break of the information contract, not a minor leak.
- `card_placed` and `row_taken` are emitted **one per card, in resolution order**, so the UI can animate a play correctly rather than snapping to the end state.

Game events record what happened, not derived statistics. `match_ended` carries final scores and the winner and nothing else — see §12.

---

### 10.4 Open question: a player's own action count under communication

§5.1 grants a player their own action count and remaining budget, and §9.2 puts
both in the view. Under classic rules this folds cleanly from the player's own
visible events. Under communication it does not: an explicit `commit` emits only
the public `player_committed`, and attaching a count to a public event would
publish an opponent's action count, which §5.2 forbids unless a budget is in
force.

Resolve this when communication is built (phase 6), not before. The likely answer
is a private counterpart to `player_committed` addressed to the actor, in the
same shape as `selection_made` alongside `selection_registered`. Whatever is
chosen, it must not make an opponent's activity observable.

---

## 11. Determinism, ordering, replay, lifecycle

### 11.1 Canonical ordering

With concurrent clients, "the same sequence of actions" needs a definition. The authoritative order is the server's processing order, recorded as `server_action_seq`. Persist an action record alongside the event log:

```json
{"server_action_seq": 207, "action_id": "b3f1-...", "player_id": "bob",
 "type": "commit", "from_view": "v_9f2c", "received_at": "..."}
```

Replay uses `server_action_seq`. Never timestamps.

**Every action that reaches the transition boundary gets a record and a sequence
number, accepted or not.** The number is assigned at §9.5 step 3, before the
transition is applied, so a rejected action consumes one and is recorded like any
other. Both surfaces do this identically: an action record file is the log of
what was *asked*, the event log is the record of what *happened*, and an
experiment wanting to know how often an agent proposed something illegal reads
the difference between them. A surface that recorded only accepted actions would
renumber the same match differently and the two logs would stop being comparable.

**A cached idempotent replay is not a second attempt** and gets no record and no
sequence number. §9.5 resolves the idempotency key at step 2, before step 3, so a
duplicate submission returns the first attempt's result and adds nothing to the
log — which is the whole point of the key. The distinction is worth stating
because it looks like a hole in the rule above and is not: a replay is one
attempt observed twice, and counting it twice would make a flaky network look
like an indecisive agent.

**Decision timing is stamped by whoever called the bot.** `received_at` is when
an action arrived, which is *after* the deciding is over, so deltas between
consecutive records measure submission gaps and include the previous
transition — not how long anything took to think. An action record therefore
carries `decision_started_at` and `decision_ended_at` as wall-clock stamps, plus
`decision_duration_ms` measured on a monotonic clock — because the difference
between two wall-clock stamps can be negative across an adjustment, and a
negative think time is worse than none. All three are optional. The surface that
offered the turn and waited fills them: the arena knows exactly when it handed a
view over and when the call returned, and the server leaves them unset, because a
client's thinking happens on the other side of a socket and anything it reported
would be a claim rather than a measurement. A consumer treats them as absent
rather than assuming zero (§12).

**A decision that returns no action still gets a record.** A call that times out
or raises produced no action, so without one the most useful facts about the
worst decisions — which view the agent held, and how long it ran before being
abandoned — are the ones not recorded. A record therefore carries an `outcome` of
`accepted`, `rejected`, `timeout` or `error`, and its action `type` may be null
when nothing was returned. The server only ever writes `accepted` and `rejected`
with a real type; the other two are shapes only a surface that calls its players
directly can produce.

### 11.2 Seeds and shuffling

- A match has one seed, supplied or generated at creation.
- Derive a per-hand seed deterministically, with `hand_number` 1-based:

```python
hand_seed = int.from_bytes(
    hashlib.sha256(f"{match_seed}:{hand_number}".encode("utf-8")).digest()[:8],
    "big",
)
deck = list(range(1, 105))
random.Random(hand_seed).shuffle(deck)
```

- Deal from `deck` exactly as §2.2 specifies.
- Use no other randomness anywhere. Never touch the global `random` module.

**What a seed fixes, and what it does not.** The seed fixes the deal;
`server_action_seq` fixes the order actions were applied. Between them a log is
authoritative, and replaying one reproduces the match exactly. It does not follow
that re-*running* a line-up reproduces a match. A scripted bot handed a derived
seed is reproducible; an LLM bot is not, and no seed makes it one. A trace is
therefore the record of what happened, and a fresh run is a fresh sample. An
experiment that reports a result must say which kind of seat produced it (§16.5).

Per-hand seeds mean two different agent line-ups can be dealt identical hands, which is what makes comparisons fair. It matters more now that matches run to 66 and therefore vary in length.

**On cross-language reproducibility.** This pins a Python implementation (CPython's Mersenne Twister and its `shuffle`), not a language-independent protocol. That is deliberate: the arena is Python, and writing out a full PRNG specification — bit-exact MT19937 initialisation, `_randbelow` rejection sampling, Fisher–Yates direction — is a lot of specification surface for a reimplementation nobody has planned.

The protection instead is **known-answer vectors**. Commit to the test suite the full shuffled deck for a small set of `(match_seed, hand_number)` pairs, and the resulting hands for 2, 5 and 10 players. Any reimplementation has a concrete target, and any Python upgrade that changes shuffle behaviour fails loudly rather than silently redealing history. If cross-language reproduction ever becomes a real requirement, replace this section with an explicit algorithm and keep the same vectors.

### 11.3 Persistence and replay

Put persistence behind an `EventSink` interface so the engine never knows whether events go to JSONL, SQLite, or anything else. Initially, one JSONL file per match under `logs/{match_id}.jsonl`, flushed as written, plus the action record file.

**Both surfaces write through this one interface**, and it therefore lives below
both of them rather than inside either (§13). A log written by the arena and a
log written by the server are the same events in the same shape, and nothing
downstream branches on which produced it. Nothing is filtered on the way in: the
log is the whole match, admin events and seeds included, and audience filtering
belongs to the moment somebody reads (§10.1).

Provide `sixnimmt replay <path>`, which folds a log into a final state and prints the result. A test must assert that replaying reproduces the live final state field for field. This is what catches state changes made without a corresponding event — the failure that quietly ruins both the UI and the analysis.

### 11.4 Abandonment and resource lifecycle

Because there is no clock, a stalled match can otherwise hold resources indefinitely: in-memory state, the per-match lock or queue, outstanding long polls, SSE subscribers, event sink handles, the idempotency cache.

`DELETE /matches/{id}` must therefore:

- durably append `match_abandoned`;
- terminate outstanding `wait` and `stream` requests with `MATCH_ABANDONED` rather than leaving them hanging until timeout;
- reject all subsequent actions with `MATCH_ABANDONED`;
- close the event sink and release per-match resources;
- retain the event log on disk.

This is a surface lifecycle requirement, not a game rule; keep it out of the
engine. The arena abandons matches too — when an action cap is hit or a seat
forfeits (§16.3, §16.4) — and appends the same `match_abandoned` event, because a
match that stopped and a match that ended must never be confused downstream.

---

## 12. Analytics are derived, not recorded

```
Events  ->  MatchSummary  ->  Analytics
```

A separate module folds the event log and the action records into whatever metrics an experiment wants: scores, wins, rejected actions, messages sent and received, card changes before commitment, commits and uncommits, decision latency from `received_at` deltas, penalty composition. None of this belongs in the game events, so adding a metric never touches the rules engine.

**It reads one format from both surfaces.** An arena log and a server log are the
same events in the same file shape (§11.3), so nothing here branches on where a
match was played. A metric written for arena experiments works on a real match
against real clients, and the reverse, without being ported.

**It is not optional, and the minimum is small.** A derivation module was easy
to defer while the only consumer was a scoreboard printed at the end of a volume
run. Once the arena is an experiment platform, traces with nothing that reads
them are just disk. So `MatchSummary` — one log and its action records folded
into per-seat scores, wins, rejections, messages and decision latencies — ships
with the arena rather than after it, and the rest of the metric surface stays
phase 5.

What the module needs from the rest of the system, and must therefore be able to
rely on: `agent_metadata` naming what played each seat (§9.1),
`server_action_seq` giving canonical order over every action that reached the
transition boundary, rejected ones included (§11.1), `decision_started_at` and
`decision_ended_at` for latency where the surface could measure them, whatever
opaque per-seat statistics a bot reported (§16.6), and the run manifest
describing the line-up and configuration that produced a set of logs (§16.5).

**A log does not always know how a match ended.** `abandoned`, `forfeited` and
`failed` all end with `match_abandoned` in the event stream, and the seat
responsible and the reason live in the manifest (§16.5). `MatchSummary`
therefore accepts the manifest entry as an optional input: given it, the summary
reports the true outcome and who caused it; without it, the summary says the
match was abandoned for an unrecorded reason, and says that it is working
without a manifest rather than guessing. A summary must never report a
forfeited match as merely abandoned while implying it knew.

---

## 13. Technology and layout

Python 3.11+, FastAPI, Pydantic v2, uvicorn, pytest. No database. In-memory state plus JSONL.

```
sixnimmt/
  common/
    text.py          # shared validation of client-supplied text
  engine/
    cards.py         # deck, bull head values
    state.py         # MatchState, RowState, PlayerState, ResolutionState (immutable)
    rules.py         # GameRules, MatchProtocol, InformationPolicy
    actions.py       # typed action model
    events.py        # typed event model
    transition.py    # transition(state, action) -> (state, events); the ONLY mutator
    resolution.py    # advance_resolution, placement rules
    audience.py      # who may see an event; the ONLY filter
    views.py         # MatchView and the other view types
    fold.py          # events -> MatchView, live and whole-log; the ONLY view builder
    replay.py        # a log -> final state
  persistence/
    sink.py          # EventSink protocol, JSONL implementation, action records, readers
  server/
    app.py routes.py auth.py store.py       # registry, per-match lock, idempotency cache
    stream.py                                # live per-viewer fan-out, long poll, SSE
  arena/
    runner.py        # drives matches in-process
    scheduling.py    # who is offered the next turn (§16.3)
    bots.py          # the Bot protocol and the bot registry
    strategies.py    # scripted bots
  analytics/summary.py
  cli.py
  tests/
```

The layering, in one direction only, enforced by a test rather than by good intentions:

| Package | May import |
|---|---|
| `common/` | nothing of ours |
| `engine/` | `common/` |
| `persistence/` | `common/`, `engine/` |
| `arena/` | `common/`, `engine/`, `persistence/` |
| `server/` | `common/`, `engine/`, `persistence/` |

`engine/` must not import from `server/`, `arena/`, or FastAPI. **`arena/` must
not import from `server/`** — the arena is the fast in-process path and pulling a
web framework into it defeats the purpose. Persistence sits below both surfaces
precisely so that "one trace format" (§1) is structural: there is one writer, and
neither surface has a private one.

Publish OpenAPI at `/openapi.json` and export JSON Schema for every action and event type, so the MCP server and UI generate types rather than duplicating them.

---

## 14. Tests

Acceptance criteria, not suggestions.

**Rules**

- Deck has 104 distinct cards totalling **171** bull heads; the group subtotals in §2.1 are each asserted.
- Bull head values are asserted from a literal table, not a reimplementation of the rules. Include at minimum 55 → 7; 11, 22, 33, **44**, 66, 77, 88, 99 → 5; 10, 50, 100 → 3; 5, 15, 25, 95 → 2; 7, 23, 41, 103 → 1.
- The §2.7 worked example reproduces exactly, **including the board after each of the four cards** and Alice's 12 heads.
- Eligibility uses strictly-lower, not lower-or-equal.
- Sixth card: a full row receiving a legal placement captures exactly 5 and the row becomes `[played_card]`.
- Too low: a card under all four ends triggers a choice, and every row is a legal answer — parametrise over all four, including a one-card row and a full five-card row.
- At most one row choice arises per play, and always on the play's lowest card (§2.8).
- Resolution order: build a play where seating order and ascending order differ, and assert ascending wins.
- Chaining, isolated: a minimal two-card play where the first card changes a row end and thereby changes the second card's eligible set. Assert the intermediate board, not just the final one.
- All §2.8 invariants hold after every placement, checked by a property test over many random plays.
- Dealing order matches §2.2 exactly for 2, 5 and 10 players, from a known deck.
- 10 players consume the deck exactly; 2 players leave 80 cards in the remainder, absent from every view including omniscient.
- A hand is exactly 10 plays; all hands empty together.
- Match ends at the end of the hand where someone reaches 66, not mid-hand; a player crossing 66 in play 3 still plays plays 4–10.
- Lowest cumulative score wins; simultaneous lows are reported as a tie. Construct the tie deliberately and assert the `match_ended` payload — a test that derives the expected winners from its own inputs asserts nothing, and a tie that depends on a lucky seed is not a test of ties.

**Commitment and concurrency**

- The play commits at the exact moment the last player commits, and not before.
- `select_card` clears the caller's committed flag and nobody else's.
- `uncommit` succeeds while another player is uncommitted and fails when the caller is the last one.
- Classic mode: `select_card` commits implicitly and `uncommit` returns `COMMUNICATION_DISABLED`.
- Four concurrent races, each producing exactly one valid serialized outcome and consistent state: final `commit` against another final `commit`; against `uncommit`; against `select_card`; against `send_message`.

**Information contract** — one test per clause of §5.1 and §5.2

- Serialise every player's view at every phase of a full match; assert no other player's hand card appears in any field. **Compare card-bearing fields specifically** — the hand, penalty piles, row contents, the selection, and revealed cards. Scraping every integer out of the serialised view instead will flag scores, counters and cursors as if they were cards, and the false positives will bury the real ones.
- Under hidden policy, no opponent card appears before `cards_revealed`.
- A direct message's content is absent from every feed except sender, recipient, and omniscient.
- With existence visible, others receive `private_message_occurred` with parties and no content.
- Undealt remainder cards appear in no view, including omniscient.
- **No seed appears in any player, public spectator, or omniscient view or event.** Grep the serialized streams for the seed value.
- A player token cannot read another match, nor the omniscient view of its own.

**Metadata inference** — the §5.3 rule

- Bob sends 20 invalid actions; Alice's `view_version` and event cursor are completely unchanged.
- With `private_message_existence = "hidden"`, Bob messages Cara; Alice's cursor, `view_version`, event count, and response shapes are indistinguishable from the case where no message was sent.
- Under hidden card selection, Bob changes his card five times; Alice's cursor advances only by the public shadow events, identically to a single change.
- **The public shadow of a re-selection does not depend on what was selected.** Bob re-selecting the card he already holds and Bob switching to a different card must produce byte-identical public events. Emitting `selection_cleared` only when the card actually differs tells the table whether a hidden selection moved.
- Every player's event stream is contiguous from 1 with no gaps, across a full match.
- Error responses carry the caller's `view_version` and never a global one.
- **Response-shape equivalence.** Run two executions differing only by events invisible to Alice. Every response Alice receives — success *and* error — is byte-identical after normalising transport metadata. Run this specifically with a private rejection by Bob, and with a hidden direct message between Bob and Cara.
- **No wake on hidden events.** Alice holds an open `/wait`; Bob sends a hidden direct message and then a rejected action. Alice's call does not return early and her cursor does not move. Same for an SSE subscriber.
- **`view_id` opacity.** Across a full match, no `view_id` is a function of the global `seq`, a global version, or a timestamp. Assert that two players observing the same public event receive different `view_id` values, and that `view_id` values do not increase in lockstep with global activity.
- **No global counters in player output.** Serialise every player and public-spectator response across a full match and recursively assert that no field named `seq`, `server_action_seq`, `global_version`, `mutation_version` or equivalent appears at any depth.
- **`legal_actions` regression guard.** For every public event type, compute each player's `legal_actions` before and after, and assert any change is accompanied by an event visible to that player. This turns the §5.3 corollary into a test rather than a claim.

**Idempotency and concurrency boundaries**

- Alice and Bob use the same `action_id`; each gets their own independent action, and neither can observe the other's cache entry.
- The same player reusing an `action_id` with a different payload gets `IDEMPOTENCY_KEY_REUSED`, not the stale result.
- The same player reusing an `action_id` with an identical payload but a different `from_view` or `expected_view_version` gets the cached result.
- Bob floods the server with 5000 distinct `action_id` values; Alice's earlier `action_id` still deduplicates correctly.
- TOCTOU: Alice submits with `expected_view_version = 87` while Bob's visible action is in flight. Exactly one ordering is realised, and if Bob's action is serialized first, Alice gets `VERSION_CONFLICT` rather than a silently applied action.
- A token used against a match it has no rights to returns `MATCH_NOT_FOUND`, identical to a match that does not exist.

**Robustness**

- Each documented error code is produced by a real scenario and leaves state unchanged.
- A duplicate `action_id` applies once and returns the original result.
- A stale `expected_view_version` is rejected.
- A full match completes against an agent that sends one illegal action before every legal one.

**Pathological agents.** The criterion is that the *server* stays healthy, not that the *game* progresses — a player who never commits legitimately prevents progress, and that is the harness's problem. Assert that the server never crashes, corrupts state, leaks hidden information, busy-loops, generates unbounded events, or deadlocks internally, against bots that: never commit; alternate commit and uncommit indefinitely; spam messages; send malformed bodies; select unheld cards; reuse action IDs; use stale versions; issue concurrent requests; repeatedly choose illegal rows.

**Liveness and lifecycle**

- A never-commit agent leaves the server healthy and the state valid indefinitely.
- The harness detects non-progress and calls `DELETE`; `match_abandoned` is appended.
- Outstanding `wait` and `stream` requests terminate with `MATCH_ABANDONED` rather than hanging.
- Subsequent actions are rejected; the log survives on disk.

**High-volume arena** — 1000 games at each of 2, 3, 5 and 10 players with random bots, asserting: no impossible states, the §2.8 invariants throughout, every player finishes each hand with zero cards, every match terminates, replay matches live state, scores non-negative and consistent, information boundaries hold. Run the same suite in communication mode with bots that message, re-select and uncommit.

**Arena** — the §16 criteria

- A bot's observation is a `MatchView` built by the same call the server makes. Assert it structurally: the §5 leak tests run unchanged over arena seats, and there is no arena type carrying card data.
- **A rejected action does not end a match.** A bot that plays one illegal action before every legal one completes a full match, in both modes, and each rejection appears as an `action_rejected` event on that seat's stream and nowhere else.
- **A rejected bot is told why.** The `rejection` passed to the retry carries the same code, message and `legal_actions` the HTTP server returns for the identical refusal — asserted against the server's own error body, not against a copy of it. A bot that corrects itself from the message completes its match; a bot that ignores it forfeits.
- A bot that returns only illegal actions exhausts its rejection budget and forfeits; the match is recorded as forfeited naming the seat and the error code, and the run continues to the next game.
- A bot that raises ends its match as `failed`, naming the seat, game index, seed and exception, and **the run continues** — asserted with a seat that raises on a known game index in a multi-game run. Under stop-on-failure the run instead stops, and says where.
- `sequential` paired with communication is refused before the first game, naming the starvation it would cause.
- **Communication terminates under the arena's caps.** Bots that never commit, and bots that message forever, both end in an abandoned match rather than a hang — and abandoned is counted separately from finished, never silently as a result.
- Under `round_robin`, a committed seat is not offered a turn until somebody uncommits; during `AWAITING_ROW_CHOICE` only the awaited player is offered one, in either mode.
- **Trace equivalence.** A log written by an arena match and a log written by a server match fold through the same `replay` and the same `MatchSummary`; a fixture of each is asserted to differ in no structural way.
- **Action records cover every attempt.** A match containing rejected actions produces records for all of them, contiguously numbered, in submission order, from both surfaces alike.
- **Concurrency changes no result.** The same run at concurrency 1 and concurrency 8, with deterministic seats, produces identical per-match logs ignoring timestamps and identical aggregates.
- Per-seat statistics reported by a bot reach the manifest unaltered; a bot that reports none, and a bot whose `stats()` raises, each produce a manifest that says so and a match that is otherwise unaffected.
- **A hung decision ends its match, not the run.** A seat that blocks past the deadline yields a `failed` match naming the timeout while other matches complete; a run that loses all its capacity to such decisions stops and says so, rather than hanging.
- **Stopping is bounded.** Under stop-on-failure, matches already running are recorded and matches not yet started are not; requested, started and completed counts differ exactly as the failure implies.
- **Limits count attempts.** A seat generating only rejected actions is stopped by the rejection budget, and a seat generating a legal action every time is stopped by the play limit at the attempt count, not the acceptance count; when both trip together, the outcome is the forfeit.
- **A decision's budget resets on the next offer**, asserted with a seat that exhausts most of its budget on several consecutive offers without forfeiting.
- **Latency is measurable.** Arena action records carry decision start and end stamps and server records leave them unset; `MatchSummary` reports latency from the former and reports it as unavailable for the latter, never as zero.
- **A summary knows what a manifest tells it.** The same forfeited match summarised with and without its manifest entry reports `forfeited` with a seat, and `abandoned` with an explicit "no manifest" note, respectively.
- An arena match's final state equals the replay of its own log, field for field, after every accepted transition and not merely at the end.
- **Incremental and whole-log folds agree.** After every appended batch, the live per-viewer folder's view equals `build_view` over the same events for that viewer.
- Deterministic line-ups reproduce byte-identically across two runs of the same root seed, ignoring timestamps. A line-up containing a non-deterministic seat reproduces the deal and nothing more, and the manifest says so.
- An unknown bot name fails before the first game is played, and lists the names that exist.

Fold the event stream independently of the engine and assert the folded board, hands, piles and scores equal the live ones after every placement. An independent fold is what proves the log is sufficient to rebuild state, which is the premise both replay and every player view rest on.

**Determinism**

- Same seed and same action sequence produce identical event logs, ignoring timestamps.
- Two matches with the same seed deal identical hands in hand 3, regardless of what happened in hands 1 and 2.
- Known-answer vectors: for fixed `(match_seed, hand_number)` pairs at 2, 5 and 10 players, the full shuffled deck, each player's hand **in deal order**, and the four starting rows reproduce exactly. Assert deal order rather than sorted hands, or the test cannot detect an implementation that deals correctly and then stores cards differently. Commit these vectors before implementation starts; they are the practical compatibility contract.
- **Event-fold board equality.** After every `card_placed` and `row_taken` event, fold the event stream from the start and assert the resulting board equals the engine's live board at that same point. This catches the implementation that mutates the board correctly but emits only final-state events, which would break both replay and UI animation while every rule test still passed.

---

## 15. Build order

**Phase 0 — model.** Define `State`, `Action`, `Event`, `View`, `GameRules`, `MatchProtocol`. Get the types right before any logic.

**Phase 1 — pure classic engine.** Cards, setup and dealing, the commit model (select / commit / uncommit, unanimity, with communication off), resolution including the row-choice pause, scoring, the 66 termination check. All rule, invariant and determinism tests green. No server.

**Phase 2 — minimal arena runner.** `sixnimmt arena --players random random --games 10000 --seed 1234`, in-process, no HTTP. Run the high-volume suite. This is what establishes that the game is actually correct. Classic only, random bots only, aggregate counters only — phase 7 generalises it into the experiment platform §16 describes.

**Phase 3 — HTTP server.** Routes, auth, roles, per-match serialisation, views, per-viewer cursors, error codes, idempotency, abandonment lifecycle.

**Phase 4 — event log and replay.** `EventSink`, JSONL, action records, SSE, `replay`. Make replay authoritative and test it hard.

**Phase 5 — analytics module.** Derived summaries over the log (§12). Deferrable only until there are experiment traces to derive from; phase 7 is what makes it load-bearing.

**Phase 6 — communication.** Switch on messaging, the information policy, direct messages, and the budget mechanism. The commit machinery is already there from Phase 1, so this phase adds communication and nothing structural.

**Phase 7 — the arena as an experiment platform** (§16). Rules and protocol as run
parameters, view-based observations, incremental folding, communication scheduling,
rejections that explain themselves, forfeit and failure outcomes, concurrent
matches, per-match traces and a run manifest, a bot registry with scripted
strategies, and `MatchSummary` so the traces have a reader. This is the phase
that gives LLM seats somewhere to plug in, and it takes the first slice of phase
5 with it.

Phases 1 and 2 are the whole game. If they are right the rest is plumbing; if they are wrong no plumbing will save it.

---

## 16. The arena

The arena runs matches in one process, with no HTTP, no sockets and no clock, so
a line-up of bots can be played many times and studied. It is the experiment
platform; §9's server is the play platform. They share the engine, the
information model and the trace format, and deliberately nothing else.

### 16.1 What a run is

A **run** is a bot line-up, a `GameRules`, a `MatchProtocol`, a root seed, and a
number of games, plus the harness settings of §16.3 and §16.6 — the scheduler,
the action limits, and how many matches run at once.

The line is worth drawing precisely, because it is easy to blur. **No game
setting is arena-only.** Anything that changes what is legal, what is visible, or
how a match ends lives in `GameRules` or `MatchProtocol`, where the server can
express it too; a rule that only exists in the arena would produce results that
say nothing about real play (§1). The scheduler and the caps are not game
settings — they decide who is *offered* a turn and when the arena gives up, and
§4 explicitly delegates the second to the harness. They are recorded in the
manifest precisely because they shape a result without being rules.

Both modes run here, and a comparison between them varies only the protocol.
Classic is `communication_enabled = false`. Communication is the same run with
messaging, revocable selections and optionally an action budget switched on —
which is exactly what §1 promised when it said classic is communication with
features off, and the arena is where that claim gets exercised rather than
asserted.

### 16.2 The bot interface

```python
class Bot(Protocol):
    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action: ...
```

- **A bot receives a `MatchView` and nothing else.** It is produced by folding that seat's audience-filtered event stream, through the same code path as `GET /state` (§9.2, §10.2). There is no arena observation type. Hand-copying a subset of `MatchState` is the projection-and-blank pattern §9.2 forbids: it passes every test written against the fields it happens to copy, and leaks the first field somebody adds later.
- **A bot may return any `Action`.** Not a narrowed union. Under communication the legal set includes `send_message`, `uncommit` and repeated `select_card`, and a bot restricted to `select_card` and `choose_row` cannot play the mode at all.
- **`legal_actions` is advisory here for the same reason it is advisory over HTTP** (§9.2). The engine revalidates everything. A bot that only ever emits actions it found in `legal_actions` is still not trusted to be right.
- **A bot is told why its last action was refused.** `rejection` is `None` on a first attempt and carries §9.4's payload — code, message, `legal_actions` — on a retry. Without it the arena would re-present a view identical to the one that produced the illegal action, and a retry loop that shows an agent nothing new is not a retry loop (§16.4).
- **A bot is trusted code, not a sandbox.** It runs in-process with the engine. The limits in §16.3 exist to stop a match running forever, not to contain hostile code, and nothing here should be read as a security boundary.
- **A bot may report opaque statistics** about its own work — calls made, tokens, latency, cost — which the arena records verbatim into the manifest and never interprets (§16.6).

Seats carry identity. Each is a `PlayerSeat` with a `display_name` and
`agent_metadata` (§9.1), and the arena fills the metadata from the bot —
strategy id and version for a scripted seat, provider and model for an LLM seat.
A trace read six months later then says what produced it, without a side channel.

LLM seats are assumed throughout, not accommodated as a special case. What such
a seat needs is exactly what is specified above and in §16.4 and §16.6: an
observation that is a real player view rather than a summary someone wrote for
bots; the whole action surface, because communication is the interesting mode;
tolerance of illegal actions *with a usable explanation*, because models produce
them; somewhere to record which model played and what it cost; survival of the
run when a provider returns an error; and honesty about reproducibility (§11.2).
The arena calls `act` and neither knows nor cares what happens inside it.

### 16.3 Scheduling

Classic has no scheduling problem: exactly one seat can act, and it is the one
the engine is waiting on. Communication has no such thing as "whose turn it is" —
during SELECTING every uncommitted player may act, repeatedly, in any order — so
the arena must choose who is offered a turn, and that choice is part of the
experiment rather than an implementation detail.

A **scheduler** answers "who acts next" from the state. Two are specified:

- `sequential` — the classic rule: the single seat the engine is waiting on, which under `select`-implies-`commit` is the first seat that has not committed. **It is valid only in classic mode.** Under communication a seat that never commits stays the first uncommitted seat forever, so every later seat starves and the play never reaches unanimity. A run that pairs `sequential` with `communication_enabled` is refused at configuration time rather than left to starve, because a starved run looks like a slow one.
- `round_robin` — during SELECTING, cycle over the seats that have not committed, resuming after the seat offered the last turn, until the play commits or the play's action limit is spent. A seat declines by committing; a committed seat is skipped until somebody uncommits. Valid in both modes, and in classic it degenerates exactly to `sequential`, since committing is what selecting does there.

`AWAITING_ROW_CHOICE` bypasses the scheduler in both modes: the engine names the
awaited player, and only that player may act (§8). This is a property of the
phase, not a policy, so it must not be something each scheduler reimplements and
one of them gets wrong.

The scheduler is recorded in the run manifest. It never enters the engine: it
decides who is *offered* a turn, and the engine alone decides what they may
legally do with it.

**Termination is the arena's problem.** §4 is explicit that with no budget and no
clock, two accommodating models can communicate forever, and that the harness must
impose the limit. In the arena, the arena is the harness:

| Limit | Counts | Resets | Effect |
|---|---|---|---|
| `match_action_limit` | attempts, accepted and rejected alike | never | match abandoned |
| `play_action_limit` | attempts in the current play | on `play_started` | match abandoned |
| `decision_rejection_limit` | consecutive rejections inside one decision | on every new offer, and on any accepted action | seat forfeits (§16.4) |

**They count attempts, not accepted actions.** The behaviour these limits exist
to bound is an agent doing work, and a rejected action is work: it costs a model
call, an engine transition and a log record. A limit that counted only accepted
actions would let a seat that never produces a legal action run forever inside
its play.

**When they are checked, and what wins.** Evaluate limits after applying a
transition and before offering the next decision, in this order:

1. If the batch contains `match_ended`, the match is **finished**. A match the engine has completed is never reported as abandoned, whatever the counters say — a limit reached by the action that won the game is not a failure to terminate.
2. If the batch contains `play_started`, reset the play counter before checking it.
3. Rejection budget → the seat forfeits.
4. `play_action_limit` → the match is abandoned.
5. `match_action_limit` → the match is abandoned.

Most specific wins, and forfeiting names a responsible seat where abandonment
does not: an outcome that says *who* is worth more to an experiment than one that
says *something*. The outcome records which limit fired.

**A failed decision ends the match immediately** (§16.4) and consumes no limit,
because there is no longer a match for a limit to bound.

These are the arena's limits across all seats, and they are distinct from
`MatchProtocol.max_actions_per_play`, which is a **game rule** the engine
enforces per player by rejecting with `ACTION_BUDGET_EXHAUSTED`. Both may be in
force at once; they are not alternatives, and because the names are close enough
to be confused, the arena's are named for what they do to a match — it stops —
rather than for what they count.

A run must never hang, and must never let a match that merely stopped be reported
as one that ended.

### 16.4 Invalid actions and failing bots

Two failures, deliberately handled differently.

**An illegal action is normal.** `on_invalid_action = "reject"` (§4) says a
rejected action is rejected, counted and retried, and never costs the turn. That
is a rule about matches, not about transport, so it binds the arena exactly as it
binds the server. An `EngineRejection` from a bot's action is caught, recorded as
an `action_rejected` event on that seat's stream and nowhere else (§10.3), and
the seat is asked again. LLM seats will do this routinely; it is not an error
condition, and it must not end a match, a game, or a run.

**The retry must tell the bot what was wrong.** This is the part that is easy to
get wrong and useless to get wrong quietly. `action_rejected` is an event, and a
`MatchView` does not report it: a viewer's cursor advances and nothing else about
the view changes. So a seat re-asked with only a refreshed view is looking at
materially the same thing that produced the illegal action, and an agent — a
model especially — will produce it again until its budget is spent. The arena
therefore passes the rejection back into `act` as §9.4's payload: the error code,
the message that states the problem and the alternative, and the caller's
`legal_actions`. This is not a new channel; it is what the server already puts in
the body of a 4xx, and the two must say the same thing for the same refusal.

A per-decision rejection budget bounds the retry loop, and **"decision" needs a
definition or the budget bounds nothing.** A decision is one offer **from the
runner**: it begins when the runner offers a seat a turn — whether the scheduler
chose that seat, or the phase forced it, as `AWAITING_ROW_CHOICE` does — and ends
when that seat's action is accepted or the seat gives up. The budget resets on
the next offer.

Defining it as a runner offer rather than a scheduler offer is load-bearing: the
scheduler is bypassed during `AWAITING_ROW_CHOICE` (§16.3), so a scheduler-scoped
definition would leave an illegal row choice with no budget at all.

A seat can therefore consume its whole budget again on every offer it receives,
which is intended — a model that recovers from one bad play should not carry a
penalty into the next — and the totals are bounded by `play_action_limit` where
it is set and by `match_action_limit` always (§16.3). Note that
`play_action_limit` is unset by default in classic, so the match limit is the
only bound there; the two are a pair, and only one of them is always present.

A seat that exhausts its budget **forfeits**: the match is abandoned, the trace
and the manifest record which seat forfeited and on what code, and the run
proceeds to the next game. One broken bot must not end an experiment.

**A bot raising an exception is different, and is not retried by the arena.** It
is a defect in the bot, or a provider failing underneath it. The match ends with
outcome `failed`, naming the seat, the game index, the seed and the exception, so
it can be reproduced — and **the run continues**. A twelve-hour experiment must
not be ended by one provider's bad minute, and a run that stops silently at game
40 of 1 000 is worse than one that records 12 failures. Whether a seat retries
its own network errors inside `act` is the bot implementation's business; the
arena does not know what an LLM is. A run may be configured to stop on the first
failure, which is what a developer debugging a scripted bot wants and what an
overnight experiment does not; §16.6 says what stopping does to matches already
running.

**Not every failure is a match's to absorb, and the distinctions are not
cosmetic.** `act` raising, returning something that is not an `Action`, or
exceeding its deadline (§16.6) ends that match and only that match.

A **scheduler** raising, or a trace or manifest that cannot be written in a run
that is being traced, stops the run: the first is the arena's own defect and the
second means the experiment is not being recorded, which is not a result. A
recording failure has nowhere durable to record itself, so it surfaces as the
run's own error rather than as a manifest entry.

**Bot construction is judged by when it fails.** A `build` that raises on the
first game stops the run, because the line-up cannot be constructed at all and
every game would repeat it. A `build` that raises later fails only that match:
construction has demonstrably worked, so this is a transient — a credential
refresh, a rate limit — and treating it as fatal would let one bad minute end an
overnight experiment for a reason indistinguishable from the ones §16.6 exists to
survive.

A bot's optional `stats()` raising is none of these: the manifest records that
the seat reported no statistics and why, and nothing else changes, because losing
a cost figure must never lose a match.

### 16.5 Traces

Every arena match writes the same JSONL event log and action record file as a
server match, through the same sink (§11.3). This is the point of the whole
arrangement:

- `sixnimmt replay` folds an arena log exactly as it folds a server log;
- §12's analytics reads one format;
- and the §5 information tests can be run over arena traces, so a leak introduced in the arena's driving loop is caught by tests that already exist.

A trace must be self-describing, so a run also writes a **manifest**: the
line-up and each seat's `agent_metadata`, the resolved rules and protocol, the
scheduler and the action limits, the root seed and each match's derived seed,
whether the line-up was fully reproducible (§11.2), whatever opaque statistics
each seat reported (§16.6), and how each match ended — finished, abandoned on a
limit, forfeited by a named seat on a named error code, or failed with a named
exception. A result reported without that is not a result.

Action records accompany every trace and cover **every attempted action**,
rejected ones included, numbered as §11.1 requires. The arena supplies the
`action_id` a client would have supplied, and sets `from_view` to the `view_id`
of the view it actually handed the bot — which it knows for certain, having built
it, where the server can only record what the client claimed.

Tracing is a run option. One log per game for a million games is not useful, so
the aggregate counters exist independently of it, and a volume run may keep only
those.

### 16.6 Running at LLM speed

A scripted seat answers in microseconds; a model answers in seconds, fails
sometimes, and costs money. All three change what the arena has to be, and none
of them changes the engine.

**Matches run concurrently; a match runs sequentially.** A thousand games of a
thousand decisions at two seconds a decision is not a run anybody waits for, and
the fix is not to make a match parallel — actions within a match are ordered, and
that ordering is the experiment. Instead, run whole matches at once. Each already
has its own derived seed and its own bot instances, so concurrency changes no
match's result and reproducibility is untouched. A run declares how many matches
may be in flight; the default is one, which is what a scripted volume run wants.

Two obligations follow. A bot instance belongs to exactly one match and is never
shared across concurrent ones. And whatever a bot shares underneath — an HTTP
client, a rate limiter, a token bucket — is the bot's to make safe; the arena
guarantees only that it will not call one instance from two matches.

**A decision has a deadline, and where the deadline is enforced decides whether
it works at all.** Concurrency without one is worse than none: the limits of
§16.3 are checked *between* decisions, so a provider call that never returns is
never checked. A run declares a per-decision timeout, after which the match ends
`failed` with that reason.

The trap is enforcing it on the wrong thread. **The thread running the match must
not be the thread calling the bot.** If it were, a hung call would block the very
code that has to mark the match failed, append its terminal events and write its
trace, and the match could never become a recorded outcome — the timeout would
detect the problem and be unable to do anything about it. So:

- **With no deadline set** — the default, and every scripted run — the bot is called inline on the match's own thread. No extra machinery, and nothing to pay for.
- **With a deadline set**, the decision runs on a separate thread and the match's thread waits on it with a timeout. On expiry the match's thread stops waiting, records the abandoned decision (§11.1), ends the match `failed`, and moves on. It was never blocked, so it can.

What the arena cannot do is cancel the call: a blocking call in a thread is not
interruptible from outside it. Two consequences follow and belong in the design
rather than in a later surprise.

- **The abandoned thread runs until it returns, and its result is discarded.** It must be a daemon thread, or the interpreter cannot exit while a call is hung. Match *capacity* is never lost, because the match's own thread always returns — what leaks is one thread, not one worker.
- **Abandoned threads are counted and bounded.** A run that exceeds its bound stops and says so. This is a guard against a leak, not against deadlock.

**The real fix belongs to the bot.** A seat that calls a model must impose its own
client-side timeout. The arena's deadline is a backstop that keeps the *run*
alive; it is not a resource guarantee, and nothing here should be read as one.

**A failing seat is a recorded outcome, not a dead run** (§16.4).

**Stopping a run is bounded only if a deadline is set.** Under stop-on-failure,
matches are submitted as capacity frees rather than all at once, so a failure
stops further submission, discards what has not started, and lets what is already
running finish and record its outcome — a match killed halfway is not a result,
and writing one into the manifest would be a fiction. But an in-flight seat with
no deadline can block that drain indefinitely. That is a real limitation rather
than a hole to design around: it is why a run with model-backed seats should
always set a deadline, and why asking to stop on failure without one deserves a
warning.

A run reports how many matches it *requested*, *started* and *completed*. They
are equal when everything was submitted, and differ when stopping or a run-fatal
error curtailed submission. They are not a failure detector — ordinary match
failures still complete, leaving all three equal.

**Cost is data.** An experiment against models is uninterpretable without knowing
how many calls each seat made, how long they took, and what they cost. The arena
must not learn what a token is, so a bot may expose opaque per-seat statistics
that the manifest records verbatim and nothing interprets — the same treatment
`agent_metadata` gets, for the same reason.

### 16.7 Command line

```
sixnimmt arena --players random random greedy --games 1000 --seed 1234 \
    --communication --scheduler round_robin --concurrency 8 --trace-dir traces/
```

Bot names resolve through a registry, which is the only place a name maps to an
implementation. An unknown name fails before the first game rather than during
it, and says what does exist.

Output reports finished, abandoned, forfeited and failed counts separately. A run
that abandoned four hundred of a thousand games while printing a clean scoreboard
is the failure the outcome vocabulary exists to make impossible.

---

## 17. What the other components need

- **MCP server.** Maps close to one-to-one onto §9.3: `get_state`, `select_card`, `commit`, `uncommit`, `send_message`, `choose_row`, `wait_for_turn`. Prefer distinct tools with precise schemas over one generic tool with a type discriminator — the schema is what steers the model. Use `legal_actions` to advertise only currently valid tools, remembering it is advisory and the server revalidates everything.
- **UI.** Consumes `/stream` with an omniscient token in development and a `public_spectator` token if games are ever shown publicly. Per-card `card_placed` and `row_taken` events let it animate resolution in true order.
- **Harness.** Owns all timing for matches played over HTTP: how long to wait on a model, when to give up, when to `DELETE` a stalled match. Reads `agent_metadata`, `server_action_seq`, and `from_view` for reproducible experiment records. Where it wants speed rather than a network, it does not reimplement any of this — it runs the arena (§16), which is the in-process path and already owns scheduling, caps and traces.
