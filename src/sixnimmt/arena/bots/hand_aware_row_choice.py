"""Choose affordable row replacements that suit the remaining hand."""

from typing import Any

from pydantic import Field, JsonValue

from sixnimmt.arena.bots._board import currently_fits, row_penalty
from sixnimmt.arena.bots.base import ActionBatch, Bot, BotOptions, Rejection
from sixnimmt.engine.actions import Action, ChooseRowAction
from sixnimmt.engine.views import MatchView, RowView


class HandAwareRowChoiceOptions(BotOptions):
    max_extra_penalty: int = Field(ge=0)
    card_strategy: str = Field(min_length=1)
    card_options: dict[str, JsonValue] = Field(default_factory=dict)


class HandAwareRowChoiceBot:
    def __init__(self, max_extra_penalty: int, card_bot: Bot) -> None:
        if type(max_extra_penalty) is not int or max_extra_penalty < 0:
            msg = "max_extra_penalty must be a non-negative integer"
            raise ValueError(msg)
        self.max_extra_penalty = max_extra_penalty
        self.card_bot = card_bot

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action | ActionBatch:
        if "choose_row" not in view.legal_actions:
            return self.card_bot.act(view, rejection)
        if view.awaiting_card is None:
            msg = "hand-aware row choice requires an awaiting card"
            raise ValueError(msg)

        cheapest_cost = min(row_penalty(row) for row in view.rows)
        maximum_cost = cheapest_cost + self.max_extra_penalty
        ranked_rows: list[tuple[int, int, int]] = []
        for candidate in view.rows:
            cost = row_penalty(candidate)
            if cost > maximum_cost:
                continue
            replaced_rows = tuple(
                RowView(index=row.index, cards=(view.awaiting_card,)) if row.index == candidate.index else row
                for row in view.rows
            )
            # Each card is assessed independently on the replacement board.
            # The played card has already left the hand at public reveal.
            fitting_count = sum(currently_fits(card, replaced_rows) for card in view.you.hand)
            ranked_rows.append((-fitting_count, cost, candidate.index))
        _, _, row_index = min(ranked_rows)
        return ChooseRowAction(row_index=row_index)

    def __getattr__(self, name: str) -> Any:
        # Preserve the delegate's optional tracing, stats, and memory hooks.
        card_bot = self.__dict__.get("card_bot")
        if card_bot is None:
            raise AttributeError(name)
        return getattr(card_bot, name)


def build_hand_aware_row_choice(
    seed: int, *, max_extra_penalty: int, card_strategy: str, card_options: dict[str, JsonValue] | None = None
) -> HandAwareRowChoiceBot:
    from sixnimmt.arena.players import PlayerConfig, resolve_players

    player = PlayerConfig(bot=card_strategy, options=card_options if card_options is not None else {})
    card_bot = resolve_players([player])[0].build(seed)
    return HandAwareRowChoiceBot(max_extra_penalty, card_bot)
