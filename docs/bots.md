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


class LowestCardBot:
    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if "choose_row" in view.legal_actions:
            return ChooseRowAction(row_index=view.rows[0].index)
        if "commit" in view.legal_actions:
            return CommitAction()
        return SelectCardAction(card=min(view.you.hand))


result = run_match([LowestCardBot(), RandomBot(22)], seed=1234)
print(result.outcome)
```

The first-row choice is intentionally simple. A competitive bot should consider
row penalties and placement risk. The bundled [greedy bot](../src/sixnimmt/arena/bots/greedy.py)
provides a more useful baseline.

## Register a configurable strategy

The registry maps names to `BotSpec` objects. A factory receives a derived seed
and validated option keywords. Put registration in your Python entry point
before invoking `run_arena`; a separate CLI process will not inherit a registry
mutation from another process.

The following extends the example above:

```python
from sixnimmt.arena.bots import REGISTRY, BotSpec
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.arena.runner import run_arena


def build_lowest(seed: int) -> LowestCardBot:
    return LowestCardBot()


REGISTRY["lowest"] = BotSpec(
    name="lowest",
    build=build_lowest,
    deterministic=True,
    metadata={"strategy_id": "lowest", "version": "1"},
)
result = run_arena([PlayerConfig(bot="lowest"), PlayerConfig(bot="random")], games=2, seed=1234)
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
