# 6 nimmt! Game Server — Implementation Spec (v4)

**Component 1 of 4.** This document specifies the game server only. The MCP server, UI, and agent harness are separate components, described here only where they constrain the server.

**Audience:** an engineer or Claude Code session implementing this from scratch. Everything needed should be here.

This version is intended to be implemented as written. The rules, the information model, and the API surface have been reviewed to convergence; §14 is the acceptance criteria.

---

## 0. What changed since v3

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

## 0.1 What changed in earlier passes

**Corrections**

- **The worked example in §2.6 was wrong, and the first correction of it was also wrong.** Card 23 is 1 head, not 2 (v2's error). Card 44 is a multiple of 11 and therefore **5 heads**, not 1 (the reviewer's error). The correct total is **12**, not 9 and not 8. Every card in the example now carries its head value inline so this cannot recur.
- **Seeds were leaking.** v2 put the match seed and hand seed in events with audience `all`, contradicting the information contract. A player holding the match seed can reproduce the shuffle and read every hand. Seeds are now admin-only.
- **Global sequence numbers were leaking.** A player could infer hidden activity from gaps in event sequence numbers and from bumps in the global state version. Players now see a gap-free, per-viewer cursor (§10.2).

**New precision**

- Exact dealing order, and `hand_number` is 1-based (§2.2).
- Board invariants stated and testable (§2.7).
- Vocabulary fixed: trick, hand, match (§2.3).
- Linearization point of the final commit stated in terms of serialization order (§7.2).
- Explicit audience classes replacing the ambiguous `"all"` (§10.1).
- Known-answer seed vectors for the shuffle (§11.2).
- Resource lifecycle for abandoned matches (§11.4).
- `from_view` defined as an audit reference, not a concurrency token (§9.3).
- `legal_actions` defined as advisory; the server is always authoritative (§9.2).
- Pathological-agent criteria distinguish internal deadlock from legitimate game non-progress (§14).

---

## 1. Purpose and scope

An HTTP server hosting games of 6 nimmt! between 2–10 players, where players are LLM agents driven by an external harness.

The server is the **single source of truth**. It owns the deck, the shuffle, the hands, the rules, and the turn structure. Clients cannot see hidden information, cannot make illegal moves, and cannot advance the game except through the actions offered to them.

Two modes, one engine:

- **Classic.** Every player secretly picks a card; picking commits it. When all have committed, cards are revealed and resolved. This is the board game, and this is what the first working version must do correctly.
- **Negotiation.** Before commitment, players exchange messages and may change their card freely. The trick proceeds only once every player has committed.

Build classic first, on the negotiation-shaped state machine. Classic mode is negotiation mode with messaging switched off and "select implies commit" — a config flag and roughly twenty lines. Retrofitting revocable selections onto a select-and-reveal loop means rewriting the trick lifecycle. Defer the *features* of negotiation; do not defer the *shape*.

### Out of scope

- Any LLM call. The server never talks to a model.
- MCP. That server is a separate process and a client of this API.
- Real authentication. Bearer tokens minted at match creation are enough.
- Rendering. The UI is a separate client.
- Analytics beyond a derivation module over the event log.

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

- A **trick** is one round of play: every player commits one card, all are revealed, all are resolved.
- A **hand** is 10 tricks. Every player plays one card per trick, so all hands empty simultaneously.
- A **match** is one or more hands, ending per §2.5.

Do not use "round" anywhere.

### 2.4 A trick

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

Then move to the next-lowest card in the trick. Each card sees the board as left by all lower cards in the same trick.

### 2.6 Scoring and ending

- A player's hand score is the total bull heads in their penalty pile.
- **Lower is better.**
- Scores accumulate across hands.
- **The match ends at the end of the first hand in which any player's cumulative score reaches 66 or more.** Check only between hands. Checking mid-hand would let the resolution order inside a single trick decide the match, which is arbitrary.
- The winner is the player with the lowest cumulative score. Report ties as ties; do not break them.

### 2.7 Worked example

Verify against this exactly, including the intermediate boards.

```
Rows before the trick:
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

---

## 3. Design principles

**The engine is a pure function.** No I/O, no network, no clock, no ambient randomness. Its whole surface is:

```
transition(state, action) -> (new_state, [events])   |   Rejection(code, message)
```

The HTTP layer authenticates, builds an action, calls the engine, persists events, and returns a filtered view. Everything worth testing is testable without a server, and the harness can later run matches in-process at full speed.

**There is no time in the engine.** No deadlines, no timers, no `now()`. A match progresses only when a player acts. Liveness is the harness's job: it decides how long to wait for a model and abandons matches that stall. Wall-clock timestamps are recorded on events for analysis but never influence a transition.

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
| `negotiation_enabled` | `false` initially | Turns on messaging and revocable selections. |
| `information_policy.card_selection` | `"hidden"` | Nobody sees another's card before reveal. |
| `information_policy.private_message_existence` | `"visible"` | Others see *that* Alice messaged Bob, never the text. |
| `allow_direct_messages` | `true` | |
| `max_actions_per_trick` | `null` | Unlimited. The mechanism exists; the bound is off. |
| `max_message_length` | `2000` | Characters. |
| `on_invalid_action` | `"reject"` | Rejected, counted, retried; never costs the turn. |
| `anonymise_display_names` | `false` | When true, players see opponents as "Player 2" etc. |

Note on the action budget: because it is `null` by default and there is no clock, **the server cannot guarantee a trick terminates**. Two accommodating models can negotiate forever. This is a deliberate choice; the harness must impose its own limit and abandon stalled matches. Implement the counting and the cap check anyway so switching it on is a config change, not a redesign. The state view always reports actions taken, and reports remaining as `null` when unlimited.

---

## 5. The information contract

This is a security specification. Every clause needs a test.

### 5.1 A player may know

- Their own hand.
- Every row's full contents.
- Every player's penalty pile and score, current and past hands.
- Cards revealed in completed tricks of the current hand.
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
| `omniscient_observer` | Every event, including all hands and all direct message content. Still not the undealt remainder. What the development UI uses. **Never issue this to an agent.** |
| `admin` | Omniscient, plus seeds, match creation and deletion. |

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
    negotiation_enabled=False,
    information_policy=InformationPolicy(...),
    max_actions_per_trick=None,
    on_invalid_action="reject",
    anonymise_display_names=False,
)
```

`target_score` sits in `GameRules` because 66 is the published rule, not an experimental parameter. `end_condition = "fixed_hands"` is a protocol-level override for controlled comparisons where variable match length would be inconvenient.

---

## 7. Match and trick state

### 7.1 Phases

```
SETUP -> SELECTING -> RESOLVING -> (next trick | next hand | FINISHED)
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

**Linearization.** Actions are serialized per match (§9.5). Once the serialized transition that accepts the final required `commit` completes, the trick is irrevocably committed and the phase is RESOLVING. No subsequent action — `uncommit`, `select_card`, or anything else — can modify the committed selections. What matters is the server's serialization order, not packet arrival time or client timestamps.

This makes committing a real commitment: you cannot commit to bait a reaction and then withdraw, unless your `uncommit` is serialized before the other player's final `commit`.

In classic mode (`negotiation_enabled = false`), `select_card` commits implicitly, `uncommit` is not offered, and messaging is disabled. Same code path, no window in which anyone can react.

### 7.3 Messages

A message has a sender, a visibility, and a body.

- `visibility = "table"` — all players.
- `visibility = "direct"` with a `to` player — sender and recipient see the content. Whether anyone else learns a message occurred is governed by `private_message_existence`.

Plain text, capped at `max_message_length`. The server does not interpret, validate, or moderate. A player claiming to hold a low card may be lying; that is the point.

Messages are legal only during SELECTING, keeping the negotiation window well defined.

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

**`POST /matches/{id}/start`** — deals the first hand, opens the first trick.
**`GET /matches`** — list with status and scores.
**`DELETE /matches/{id}`** — abandon (§11.4). This is how the harness disposes of a stalled negotiation.

### 9.2 Reading state

**`GET /matches/{id}/state`** returns the caller's view:

```json
{
  "match_id": "m_01",
  "view_version": 87,
  "view_id": "v_9f2c",
  "status": "in_progress",
  "phase": "selecting",
  "hand_number": 2,
  "trick_number": 5,
  "you": {
    "player_id": "alice",
    "hand": [4, 19, 62, 77, 91, 103],
    "selection": 62,
    "committed": false,
    "penalty_cards": [23, 25, 30, 41, 44],
    "score_this_hand": 12,
    "total_score": 27,
    "actions_taken_this_trick": 6,
    "actions_remaining_this_trick": null
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
- `actions_remaining_this_trick` is `null` when no budget is in force.
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

Minimum codes: `MATCH_NOT_FOUND`, `NOT_AUTHORIZED`, `MATCH_NOT_STARTED`, `MATCH_ALREADY_STARTED`, `MATCH_FINISHED`, `MATCH_ABANDONED`, `WRONG_PHASE`, `NOT_YOUR_TURN`, `CARD_NOT_IN_HAND`, `NO_SELECTION_TO_COMMIT`, `CANNOT_UNCOMMIT_WHEN_ALL_COMMITTED`, `NEGOTIATION_DISABLED`, `INVALID_ROW_INDEX`, `ROW_ALREADY_CHOSEN`, `ACTION_BUDGET_EXHAUSTED`, `MESSAGE_TOO_LONG`, `DIRECT_MESSAGES_DISABLED`, `RECIPIENT_NOT_FOUND`, `VERSION_CONFLICT`, `IDEMPOTENCY_KEY_REUSED`, `MALFORMED_REQUEST`, `UNKNOWN_ACTION_TYPE`.

`MATCH_NOT_FOUND` and `NOT_AUTHORIZED` must be **indistinguishable** when a token is used against a match it has no rights to: same code, same shape, same latency class. Otherwise a token becomes a probe for which match IDs exist. Return `MATCH_NOT_FOUND` for both.

Always include current `legal_actions`. It is the single most useful thing to hand a confused agent. Write messages that state the problem and the correct alternative: "the trick is resolving and the game is waiting for bob to choose a row" beats "invalid action". Respect §5.5 in every message and in the `view_version` carried.

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
  "trick": 5,
  "audience": "public",
  "data": {"player_id": "bob"}
}
```

| Type | Audience | Notes |
|---|---|---|
| `match_created` | public | rules, protocol, players — **no seed** |
| `match_seed_assigned` | admin | the match seed |
| `match_started` | public | |
| `hand_started` | public | hand number — **no seed** |
| `hand_seed_assigned` | admin | the derived hand seed |
| `cards_dealt` | `player:<id>` | one event per player, their hand only |
| `rows_initialised` | public | the four starting cards |
| `trick_started` | public | trick number |
| `selection_made` | `player:<id>` under hidden policy; public under public policy | the card |
| `selection_registered` | public | that a selection exists, no card |
| `selection_cleared` | public | on change or uncommit |
| `player_committed` | public | |
| `player_uncommitted` | public | |
| `message_sent` (table) | public | content |
| `message_sent` (direct) | two events: one `player:<sender>`, one `player:<recipient>` | each carries the content |
| `private_message_occurred` | public | parties only, no content; emitted only when existence is visible |
| `trick_committed` | public | the moment unanimity was reached |
| `cards_revealed` | public | every player's card |
| `card_placed` | public | card, row index, resulting row |
| `row_taken` | public | player, row, captured cards, heads, reason (`sixth_card` / `too_low`) |
| `row_choice_required` | `player:<id>` | |
| `row_choice_made` | public | |
| `trick_ended` | public | per-player penalty this trick |
| `hand_ended` | public | hand scores and running totals |
| `match_ended` | public | final scores, winner or winners |
| `match_abandoned` | public | |
| `action_rejected` | `player:<id>` | code and description |

Three rules that are easy to violate and expensive to fix:

- Never put hidden data in a `public` event assuming the API layer will strip it. The audience field is the only filter.
- Seeds appear only in `admin` events and the admin creation response. A player holding the match seed can reproduce every shuffle and read every hand; this is a total break of the information contract, not a minor leak.
- `card_placed` and `row_taken` are emitted **one per card, in resolution order**, so the UI can animate a trick correctly rather than snapping to the end state.

Game events record what happened, not derived statistics. `match_ended` carries final scores and the winner and nothing else — see §12.

---

## 11. Determinism, ordering, replay, lifecycle

### 11.1 Canonical ordering

With concurrent clients, "the same sequence of actions" needs a definition. The authoritative order is the server's processing order, recorded as `server_action_seq`. Persist an action record alongside the event log:

```json
{"server_action_seq": 207, "action_id": "b3f1-...", "player_id": "bob",
 "type": "commit", "from_view": "v_9f2c", "received_at": "..."}
```

Replay uses `server_action_seq`. Never timestamps.

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

Per-hand seeds mean two different agent line-ups can be dealt identical hands, which is what makes comparisons fair. It matters more now that matches run to 66 and therefore vary in length.

**On cross-language reproducibility.** This pins a Python implementation (CPython's Mersenne Twister and its `shuffle`), not a language-independent protocol. That is deliberate: the arena is Python, and writing out a full PRNG specification — bit-exact MT19937 initialisation, `_randbelow` rejection sampling, Fisher–Yates direction — is a lot of specification surface for a reimplementation nobody has planned.

The protection instead is **known-answer vectors**. Commit to the test suite the full shuffled deck for a small set of `(match_seed, hand_number)` pairs, and the resulting hands for 2, 5 and 10 players. Any reimplementation has a concrete target, and any Python upgrade that changes shuffle behaviour fails loudly rather than silently redealing history. If cross-language reproduction ever becomes a real requirement, replace this section with an explicit algorithm and keep the same vectors.

### 11.3 Persistence and replay

Put persistence behind an `EventSink` interface so the engine never knows whether events go to JSONL, SQLite, or anything else. Initially, one JSONL file per match under `logs/{match_id}.jsonl`, flushed as written, plus the action record file.

Provide `sixnimmt replay <path>`, which folds a log into a final state and prints the result. A test must assert that replaying reproduces the live final state field for field. This is what catches state changes made without a corresponding event — the failure that quietly ruins both the UI and the analysis.

### 11.4 Abandonment and resource lifecycle

Because there is no clock, a stalled match can otherwise hold resources indefinitely: in-memory state, the per-match lock or queue, outstanding long polls, SSE subscribers, event sink handles, the idempotency cache.

`DELETE /matches/{id}` must therefore:

- durably append `match_abandoned`;
- terminate outstanding `wait` and `stream` requests with `MATCH_ABANDONED` rather than leaving them hanging until timeout;
- reject all subsequent actions with `MATCH_ABANDONED`;
- close the event sink and release per-match resources;
- retain the event log on disk.

This is a server lifecycle requirement, not a game rule; keep it out of the engine.

---

## 12. Analytics are derived, not recorded

```
Events  ->  MatchSummary  ->  Analytics
```

A separate module folds the event log and the action records into whatever metrics an experiment wants: scores, wins, rejected actions, messages sent and received, card changes before commitment, commits and uncommits, decision latency from `received_at` deltas, penalty composition. None of this belongs in the game events, so adding a metric never touches the rules engine.

---

## 13. Technology and layout

Python 3.11+, FastAPI, Pydantic v2, uvicorn, pytest. No database. In-memory state plus JSONL.

```
sixnimmt/
  engine/
    cards.py         # deck, bull head values
    state.py         # MatchState, RowState, PlayerState, ResolutionState (immutable)
    rules.py         # GameRules, MatchProtocol, InformationPolicy
    actions.py       # typed action model
    events.py        # typed event model
    transition.py    # transition(state, action) -> (state, events); the ONLY mutator
    resolution.py    # advance_resolution, placement rules
    views.py         # state -> role-filtered view + per-viewer cursor; the ONLY filter
  server/
    app.py routes.py auth.py store.py       # registry, per-match lock, idempotency cache
    sink.py                                  # EventSink: JSONL impl, SSE fan-out
  arena/
    runner.py bots.py                        # in-process matches, scripted/random agents
  analytics/summary.py
  cli.py
  tests/
```

`engine/` must not import from `server/` or FastAPI. Enforce it with a test; it will matter when the harness runs matches in-process.

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
- Too low: a card under all four ends triggers a choice, and every row is a legal answer, including a one-card row.
- Resolution order: build a trick where seating order and ascending order differ, and assert ascending wins.
- Chaining, isolated: a minimal two-card trick where the first card changes a row end and thereby changes the second card's eligible set. Assert the intermediate board, not just the final one.
- All §2.8 invariants hold after every placement, checked by a property test over many random tricks.
- Dealing order matches §2.2 exactly for 2, 5 and 10 players, from a known deck.
- 10 players consume the deck exactly; 2 players leave 80 cards in the remainder, absent from every view including omniscient.
- A hand is exactly 10 tricks; all hands empty together.
- Match ends at the end of the hand where someone reaches 66, not mid-hand; a player crossing 66 in trick 3 still plays tricks 4–10.
- Lowest cumulative score wins; simultaneous lows are reported as a tie.

**Commitment and concurrency**

- The trick commits at the exact moment the last player commits, and not before.
- `select_card` clears the caller's committed flag and nobody else's.
- `uncommit` succeeds while another player is uncommitted and fails when the caller is the last one.
- Classic mode: `select_card` commits implicitly and `uncommit` returns `NEGOTIATION_DISABLED`.
- Four concurrent races, each producing exactly one valid serialized outcome and consistent state: final `commit` against another final `commit`; against `uncommit`; against `select_card`; against `send_message`.

**Information contract** — one test per clause of §5.1 and §5.2

- Serialise every player's view at every phase of a full match; assert no other player's hand card appears in any field.
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

**High-volume arena** — 1000 games at each of 2, 3, 5 and 10 players with random bots, asserting: no impossible states, the §2.8 invariants throughout, every player finishes each hand with zero cards, every match terminates, replay matches live state, scores non-negative and consistent, information boundaries hold.

**Determinism**

- Same seed and same action sequence produce identical event logs, ignoring timestamps.
- Two matches with the same seed deal identical hands in hand 3, regardless of what happened in hands 1 and 2.
- Known-answer vectors: for fixed `(match_seed, hand_number)` pairs at 2, 5 and 10 players, the full shuffled deck, each player's hand **in deal order**, and the four starting rows reproduce exactly. Assert deal order rather than sorted hands, or the test cannot detect an implementation that deals correctly and then stores cards differently. Commit these vectors before implementation starts; they are the practical compatibility contract.
- **Event-fold board equality.** After every `card_placed` and `row_taken` event, fold the event stream from the start and assert the resulting board equals the engine's live board at that same point. This catches the implementation that mutates the board correctly but emits only final-state events, which would break both replay and UI animation while every rule test still passed.

---

## 15. Build order

**Phase 0 — model.** Define `State`, `Action`, `Event`, `View`, `GameRules`, `MatchProtocol`. Get the types right before any logic.

**Phase 1 — pure classic engine.** Cards, setup and dealing, the commit model (select / commit / uncommit, unanimity, with negotiation off), resolution including the row-choice pause, scoring, the 66 termination check. All rule, invariant and determinism tests green. No server.

**Phase 2 — arena runner.** `sixnimmt arena --players random random --games 10000 --seed 1234`, in-process, no HTTP. Run the high-volume suite. This is what establishes that the game is actually correct.

**Phase 3 — HTTP server.** Routes, auth, roles, per-match serialisation, views, per-viewer cursors, error codes, idempotency, abandonment lifecycle.

**Phase 4 — event log and replay.** `EventSink`, JSONL, action records, SSE, `replay`. Make replay authoritative and test it hard.

**Phase 5 — analytics module.** Derived summaries over the log.

**Phase 6 — negotiation.** Switch on messaging, the information policy, direct messages, and the budget mechanism. The commit machinery is already there from Phase 1, so this phase adds communication and nothing structural.

Phases 1 and 2 are the whole game. If they are right the rest is plumbing; if they are wrong no plumbing will save it.

---

## 16. What the other components need

- **MCP server.** Maps close to one-to-one onto §9.3: `get_state`, `select_card`, `commit`, `uncommit`, `send_message`, `choose_row`, `wait_for_turn`. Prefer distinct tools with precise schemas over one generic tool with a type discriminator — the schema is what steers the model. Use `legal_actions` to advertise only currently valid tools, remembering it is advisory and the server revalidates everything.
- **UI.** Consumes `/stream` with an omniscient token in development and a `public_spectator` token if games are ever shown publicly. Per-card `card_placed` and `row_taken` events let it animate resolution in true order.
- **Harness.** Calls the engine in-process for speed, and owns all timing: how long to wait on a model, when to give up, when to `DELETE` a stalled match. Reads `agent_metadata`, `server_action_seq`, and `from_view` for reproducible experiment records.
