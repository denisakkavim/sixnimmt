"""Invite a cheap pickup with a low-penalty replacement card."""

from typing import Any

from pydantic import Field, JsonValue

from sixnimmt.arena.bots._board import cheapest_row, row_penalty
from sixnimmt.arena.bots.base import ActionBatch, Bot, BotOptions, Rejection
from sixnimmt.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.cards import bull_heads
from sixnimmt.engine.views import MatchView


class ControlledBurnOptions(BotOptions):
    K: int = Field(ge=0)
    fallback_strategy: str = Field(min_length=1)
    fallback_options: dict[str, JsonValue] = Field(default_factory=dict)


class ControlledBurnBot:
    def __init__(self, K: int, fallback: Bot) -> None:
        if type(K) is not int or K < 0:
            msg = "K must be a non-negative integer"
            raise ValueError(msg)
        self.K = K
        self.fallback = fallback

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action | ActionBatch:
        if "choose_row" in view.legal_actions:
            return ChooseRowAction(row_index=cheapest_row(view.rows).index)
        if "commit" in view.legal_actions:
            return CommitAction()
        lowest_end = min(row.cards[-1] for row in view.rows)
        candidates = [card for card in view.you.hand if card < lowest_end]
        if candidates and row_penalty(cheapest_row(view.rows)) <= self.K:
            card = min(candidates, key=lambda card: (bull_heads(card), card))
            return SelectCardAction(card=card)
        return self.fallback.act(view, rejection)

    def __getattr__(self, name: str) -> Any:
        # Preserve optional tracing, statistics, and transactional-memory hooks.
        fallback = self.__dict__.get("fallback")
        if fallback is None:
            raise AttributeError(name)
        return getattr(fallback, name)


def build_controlled_burn(
    seed: int, *, K: int, fallback_strategy: str, fallback_options: dict[str, JsonValue] | None = None
) -> ControlledBurnBot:
    # Resolve lazily because player configuration depends on the bot registry.
    from sixnimmt.arena.players import PlayerConfig, resolve_players

    player = PlayerConfig(bot=fallback_strategy, options=fallback_options if fallback_options is not None else {})
    fallback = resolve_players([player])[0].build(seed)
    return ControlledBurnBot(K, fallback)
