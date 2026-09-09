# Writing bots

Implement `act(view, rejection=None)` and return an engine action or an
`ActionBatch`. The arena supplies the current player's filtered `MatchView`.
A refusal retries that seat with a refreshed view and a `Rejection` containing
the code, message, legal actions, and optionally the rejected action.

This minimal strategy works in classic and communication modes:

```python
from sixnimmt.arena.bots import RandomBot, Rejection
from sixnimmt.arena.runner import run_match
from sixnimmt.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.views import MatchView


class SimpleBot:
    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if "choose_row" in view.legal_actions:
            return ChooseRowAction(row_index=view.rows[0].index)
        if "commit" in view.legal_actions:
            return CommitAction()
        return SelectCardAction(card=min(view.you.hand))


result = run_match([SimpleBot(), RandomBot(22)], seed=1234)
print(result.outcome)
```

The first-row choice is intentionally simple. A competitive bot should consider
row penalties and placement risk. The bundled [lowest fitting card bot](../src/sixnimmt/arena/bots/lowest_fitting_card.py)
plays the lowest card that currently fits. If none fits, it minimises immediate
pickup cost, breaking ties by lowest card. Its Python class is
`LowestFittingCardBot` and its registry name is `lowest_fitting_card` (formerly
`GreedyBot` and `greedy`).

All bundled baseline bots take the row with the fewest bull heads, breaking ties
by row index. `RandomBot` selects cards uniformly using a private seeded RNG;
row choices do not consume randomness. This is random strategy version 2, so
seeded matches can differ from version 1, which also chose rows randomly.

## Built-in baselines and board-and-hand heuristics

An [example player file](../examples/arena-baseline-players.json) includes all eight
strategies for a mixed arena.

All these strategies accept no options and are deterministic for a given seed.
Only `random` uses the seed; the other strategies follow fixed rules. Each uses
the cheapest-row rule and selects then commits in communication mode without
sending messages.

| Registry name | Python class | Card selection |
| --- | --- | --- |
| `random` | `RandomBot` | Uniform random card |
| `lowest_card` | `LowestCardBot` | Lowest card in hand |
| `highest_card` | `HighestCardBot` | Highest card in hand |
| `lowest_fitting_card` | `LowestFittingCardBot` | Lowest currently fitting card |
| `highest_fitting_card` | `HighestFittingCardBot` | Highest currently fitting card |
| `closest_gap` | `ClosestGapBot` | Smallest gap to the applicable non-full row, then lowest card |
| `coldest_row` | `ColdestRowBot` | Fewest cards in the applicable non-full row, then lowest card |
| `hand_flexibility` | `HandFlexibilityBot` | Minimum immediate cost, then remaining-hand coverage distance, then lowest card |

A currently fitting card targets the row with the greatest end below it, and that
row must contain fewer than five cards. Earlier opponent placements can change
whether it fits. The four fitting-based heuristics fall back to minimum immediate
pickup cost, then lowest card, when no card currently fits. This also applies to
`highest_fitting_card`: only its fitting-card preference is reversed.

Hand flexibility measures the average distance from every value in `1..104` to
its nearest remaining card. Lower is better; seen values remain included. It skips
coverage on the final card and never accepts extra immediate penalties for better
coverage. These rules are fixed. See the [strategy catalogue](strategy-families.md)
for the experimental rationale.

## Register a configurable strategy

The registry maps names to `BotSpec` objects. A factory receives a derived seed
and validated option keywords. Put registration in your Python entry point
before invoking `run_arena`; a separate CLI process will not inherit a registry
mutation from another process.

For `RunConfig(backend="process")`, define the factory and options model at
module scope in an importable Python module. Resolved definitions and options
are sent to each worker; bot instances are constructed there and do not need to
be picklable. Lambdas and local definitions are unsupported. Protect the script's
`run_arena` call with a `__main__` guard; see [process execution](arena.md#timeouts-and-concurrency).

The following extends the example above:

```python
from sixnimmt.arena.bots import REGISTRY, BotSpec
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.arena.runner import run_arena


def build_simple(seed: int) -> SimpleBot:
    return SimpleBot()


REGISTRY["simple"] = BotSpec(
    name="simple",
    build=build_simple,
    deterministic=True,
    metadata={"strategy_id": "simple", "version": "1"},
)
result = run_arena([PlayerConfig(bot="simple"), PlayerConfig(bot="random")], games=2, seed=1234)
```

For configurable factories, subclass `BotOptions` with Pydantic fields and pass
it as `BotSpec.options_model`. Defaults are resolved before construction and
recorded in the manifest. Factories receive independent copies of options and
fresh instances are built for each seat of each match. Use a private seeded RNG
for random choices. Mark a strategy deterministic only if its supported
configurations honor that promise; external model answers do not.

An optional `stats()` method may return JSON-compatible diagnostic data for the
manifest. Statistics are not part of the gameplay contract, and a statistics
exception does not change a result.

## Atomic proposals

`ActionBatch(actions=(...), memory=None)` groups several actions into one
proposal. The arena validates all game actions on temporary states before
publishing them. An illegal action rejects the proposal without applying earlier
actions or changing memory. A batch exceeding the remaining arena attempt budget
is not partially applied.

Commitment, a row choice, or any change of hand/play/phase must end the sequence
of game actions. In classic mode, selection also commits, so it ends that sequence.
For communication, selecting then committing is a useful batch:

```python
from sixnimmt.arena.bots import ActionBatch
from sixnimmt.engine.actions import CommitAction, SelectCardAction


def select_and_commit(card: int) -> ActionBatch:
    return ActionBatch(actions=(SelectCardAction(card=card), CommitAction()))
```

The optional memory field is used by the memory-capable LLM adapter. `None`
preserves memory; an empty string clears it. A memory update counts as one arena
attempt but is not an engine action. See [LLM players](llm-players.md).

Bot exceptions and malformed returns fail the match. Repeated engine rejections
forfeit it at the configured limit. Test custom strategies against short,
fixed-hand matches before starting large runs.

Source: [bot contracts](../src/sixnimmt/arena/bots/base.py) and
[transaction validation](../src/sixnimmt/arena/transactions.py).
