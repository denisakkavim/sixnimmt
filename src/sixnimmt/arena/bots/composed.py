"""Composed bot implementations and their supporting types."""

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal

from pydantic import Field, JsonValue

from sixnimmt.arena.bots.base import (
    ActionBatch,
    Bot,
    BotOptions,
    Rejection,
    ResolveStrategy,
    StrategyConstruction,
    observe_bot,
)
from sixnimmt.arena.bots.heuristics import applicable_row, cheapest_row, currently_fits, row_penalty
from sixnimmt.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.cards import bull_heads
from sixnimmt.engine.views import MatchView, RowView

if TYPE_CHECKING:
    from sixnimmt.arena.players import ResolvedStrategy


class ControlledBurnOptions(BotOptions):
    K: int = Field(ge=0)
    fallback_strategy: str = Field(min_length=1)
    fallback_options: dict[str, JsonValue] = Field(default_factory=dict)


class ControlledBurnBot(Bot):
    def __init__(self, K: int, fallback: Bot) -> None:
        if type(K) is not int or K < 0:
            msg = "K must be a non-negative integer"
            raise ValueError(msg)
        self.K = K
        self.fallback = fallback

    def observe(self, view: MatchView) -> None:
        observe_bot(self.delegate_bot(), view)

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action | ActionBatch:
        self.observe(view)
        if "choose_row" in view.legal_actions:
            return ChooseRowAction(row_index=cheapest_row(view.rows).index)
        if "commit" in view.legal_actions:
            return CommitAction()
        lowest_end = min(row.cards[-1] for row in view.rows)
        candidates = [card for card in view.you.hand if card < lowest_end]
        if len(candidates) > 0 and row_penalty(cheapest_row(view.rows)) <= self.K:
            card = min(candidates, key=lambda card: (bull_heads(card), card))
            return SelectCardAction(card=card)
        return self.fallback.act(view, rejection)

    def delegate_bot(self) -> Bot:
        return self.fallback


CandidateRanking = Literal["most_intervening", "cheapest_pickup", "highest_card"]


class CountThresholdBaitOptions(BotOptions):
    intervening_card_threshold: int = Field(gt=0)
    candidate_ranking: CandidateRanking
    fallback_strategy: str = Field(min_length=1)
    fallback_options: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass(frozen=True)
class _Candidate:
    card: int
    intervening: int
    pickup_cost: int


class CountThresholdBaitBot(Bot):
    def __init__(self, intervening_card_threshold: int, candidate_ranking: CandidateRanking, fallback: Bot) -> None:
        if type(intervening_card_threshold) is not int or intervening_card_threshold < 1:
            msg = "intervening_card_threshold must be a positive integer"
            raise ValueError(msg)
        if candidate_ranking not in ("most_intervening", "cheapest_pickup", "highest_card"):
            msg = f"unknown candidate_ranking: {candidate_ranking!r}"
            raise ValueError(msg)
        self.intervening_card_threshold = intervening_card_threshold
        self.candidate_ranking = candidate_ranking
        self.fallback = fallback

    def observe(self, view: MatchView) -> None:
        observe_bot(self.delegate_bot(), view)

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action | ActionBatch:
        self.observe(view)
        if "choose_row" in view.legal_actions:
            return ChooseRowAction(row_index=cheapest_row(view.rows).index)
        if "commit" in view.legal_actions:
            return CommitAction()

        unavailable = set(view.you.hand)
        unavailable.update(card for row in view.rows for card in row.cards)
        unavailable.update(card for play in view.revealed_this_hand for card in play)
        # Captured row-start cards may never have appeared in a card reveal.
        unavailable.update(view.you.penalty_cards)
        unavailable.update(card for player in view.players for card in player.penalty_cards)
        candidates: list[_Candidate] = []
        for card in view.you.hand:
            row = applicable_row(card, view.rows)
            if row is None or len(row.cards) != 5:
                continue
            intervening = sum(value not in unavailable for value in range(row.cards[-1] + 1, card))
            if intervening >= self.intervening_card_threshold:
                candidates.append(_Candidate(card, intervening, row_penalty(row)))
        if len(candidates) == 0:
            return self.fallback.act(view, rejection)
        selected = min(candidates, key=self._rank)
        return SelectCardAction(card=selected.card)

    def _rank(self, candidate: _Candidate) -> tuple[int, ...]:
        if self.candidate_ranking == "most_intervening":
            return (-candidate.intervening, candidate.pickup_cost, candidate.card)
        if self.candidate_ranking == "cheapest_pickup":
            return (candidate.pickup_cost, candidate.card)
        return (-candidate.card,)

    def delegate_bot(self) -> Bot:
        return self.fallback


class HandAwareRowChoiceOptions(BotOptions):
    max_extra_penalty: int = Field(ge=0)
    card_strategy: str = Field(min_length=1)
    card_options: dict[str, JsonValue] = Field(default_factory=dict)


class HandAwareRowChoiceBot(Bot):
    def __init__(self, max_extra_penalty: int, card_bot: Bot) -> None:
        if type(max_extra_penalty) is not int or max_extra_penalty < 0:
            msg = "max_extra_penalty must be a non-negative integer"
            raise ValueError(msg)
        self.max_extra_penalty = max_extra_penalty
        self.card_bot = card_bot

    def observe(self, view: MatchView) -> None:
        observe_bot(self.delegate_bot(), view)

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action | ActionBatch:
        self.observe(view)
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

    def delegate_bot(self) -> Bot:
        return self.card_bot


def resolve_composed(options: BotOptions, resolve: ResolveStrategy) -> StrategyConstruction:
    if isinstance(options, HandAwareRowChoiceOptions):
        delegate = resolve(options.card_strategy, options.card_options)
        return StrategyConstruction(
            partial(_build_hand_aware, options=options, delegate=delegate),
            delegate.deterministic,
            {"card_strategy_metadata": delegate.metadata},
            {"card_options": delegate.recorded_options},
        )
    if isinstance(options, (ControlledBurnOptions, CountThresholdBaitOptions)):
        delegate = resolve(options.fallback_strategy, options.fallback_options)
        return StrategyConstruction(
            partial(_build_bait, options=options, delegate=delegate),
            delegate.deterministic,
            {"fallback_metadata": delegate.metadata},
            {"fallback_options": delegate.recorded_options},
        )
    msg = f"unsupported composed options: {type(options).__name__}"
    raise TypeError(msg)


def _build_hand_aware(seed: int, *, options: HandAwareRowChoiceOptions, delegate: "ResolvedStrategy") -> Bot:
    return HandAwareRowChoiceBot(options.max_extra_penalty, delegate.build(seed))


def _build_bait(
    seed: int, *, options: ControlledBurnOptions | CountThresholdBaitOptions, delegate: "ResolvedStrategy"
) -> Bot:
    if isinstance(options, ControlledBurnOptions):
        return ControlledBurnBot(options.K, delegate.build(seed))
    return CountThresholdBaitBot(options.intervening_card_threshold, options.candidate_ranking, delegate.build(seed))
