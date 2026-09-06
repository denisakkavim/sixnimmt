# Game rules and information

## Cards, hands, and matches

A **play** consists of each player committing one card, followed by revealing and
resolving those cards. A **hand** contains ten plays. A **match** contains one or
more hands. There are two to ten players, 104 unique cards numbered 1–104, and
four rows with a capacity of five cards each.

Cards carry penalty points called bull heads. Apply the first matching rule:

| Card | Bull heads |
| --- | ---: |
| 55 | 7 |
| Other multiples of 11 | 5 |
| Multiples of 10 | 3 |
| Other multiples of 5 | 2 |
| All others | 1 |

The complete deck contains 171 bull heads. Each hand starts with a seeded shuffle.
Cards are dealt one at a time in seating order for ten passes. The next four
cards start rows 0–3. Remaining cards are unused for that hand. Hands are stored
in deal order, though a bot's presentation may sort them.

## Resolving a play

Once all players commit, their selected cards are revealed together and placed
in ascending card order. Each card sees the board left by earlier placements.

1. Find rows whose last card is strictly lower than the played card.
2. If any qualify, use the row with the highest qualifying last card.
3. If that row already has five cards, the player captures all five as penalties
   and the played card becomes the new row start. Otherwise, append the card.
4. If no row qualifies, pause for that player to choose any row. They capture its
   entire contents, replace it with their card, and resolution continues.

For example, with rows `[7]`, `[23, 25, 30, 41, 44]`, `[52]`, `[88]`, resolve
cards 3, 45, 46, 53 in that order. If the player of 3 chooses row 0, they take one
point. The player of 45 takes row 1 for 12 points. Card 46 then appends to 45,
and 53 appends to 52. Row choices use zero-based indices.

At hand end, penalties are added to cumulative scores. By default the match ends
after a completed hand brings any player to at least 66 points. It never ends
mid-hand for reaching that threshold. All players sharing the lowest final score
are winners. `MatchProtocol(end_condition="fixed_hands", hands=N)` instead plays
exactly N hands, ignoring the target-score termination condition.

## Classic and communication modes

In classic mode, selecting a card commits it automatically. Communication mode
separates selection from commitment and permits messages during selection:

| Action class | Effect |
| --- | --- |
| `SelectCardAction(card=...)` | Select or revise a card held by the player |
| `CommitAction()` | Commit the current selection |
| `UncommitAction()` | Withdraw commitment while selection remains open |
| `SendMessageAction(visibility="table", body=...)` | Send a table message |
| `SendMessageAction(visibility="direct", to_player=..., body=...)` | Send to another player |
| `ChooseRowAction(row_index=...)` | Answer a pending row choice |

The final commitment immediately starts resolution; it cannot subsequently be
withdrawn. Bundled arena schedulers skip committed seats, so they do not give
those seats an opportunity to initiate an uncommit. The engine still supports
the action for callers implementing their own scheduling.

Messages are optional. Direct messages can be disabled. Self-directed messages
are rejected; empty bodies are allowed. The default message limit is 2,000
Unicode characters, and text must be UTF-8 representable. Required row choices
block selection and messaging until answered.

`MatchProtocol.max_actions_per_play` optionally limits each player's selections,
commits, uncommits, and messages. Required row choices remain available at zero
budget. This is separate from the arena's attempt limits; see [Running arenas](arena.md).

## Information boundaries

Bots receive a `MatchView` built from their audience-filtered events. They see
their own hand and selection, the public rows and scores, public commitments,
revealed cards, and messages they are permitted to receive. They do not receive
other hands, hidden selections, the match seed, or the undealt remainder.

| Viewer role | Event access |
| --- | --- |
| Player | Public events and events addressed to that player |
| Public spectator | Public events only |
| Omniscient observer | Public and all player events, but no admin events |
| Admin | All events, including seed and creation records |

Direct-message contents are visible to participants and privileged observers.
By default, other players can see that a direct message occurred and who its
parties were. Set `InformationPolicy(private_message_existence="hidden")` on the
protocol to hide even that occurrence. Private events must not advance an
uninvolved player's view cursor. Action budgets are private too.

Views retain up to 100 current-play messages/occurrences, 100 messages in
cross-hand `message_history`, and 20 revealed plays in `play_history`. Public
play history includes player attribution, placement rows, and captured cards.
These bounded windows are observations, not full experiment logs.

`legal_actions` is advisory; the engine validates each proposed action against
the current state. An arena observer callback receives authoritative state and
must be treated as privileged instrumentation. In-process bots are trusted
Python code, not a security sandbox.

Sources: [rules](../src/sixnimmt/engine/rules.py),
[actions](../src/sixnimmt/engine/actions.py),
[resolution](../src/sixnimmt/engine/resolution.py), and
[audience filtering](../src/sixnimmt/engine/audience.py).
