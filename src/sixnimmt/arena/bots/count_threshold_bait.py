"""Bait full rows using counts of possible intervening opponent cards."""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, JsonValue

from sixnimmt.arena.bots._board import applicable_row, cheapest_row, row_penalty
from sixnimmt.arena.bots.base import ActionBatch, Bot, BotOptions, Rejection
from sixnimmt.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.views import MatchView

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


class CountThresholdBaitBot:
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

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action | ActionBatch:
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
        if not candidates:
            return self.fallback.act(view, rejection)
        selected = min(candidates, key=self._rank)
        return SelectCardAction(card=selected.card)

    def _rank(self, candidate: _Candidate) -> tuple[int, ...]:
        if self.candidate_ranking == "most_intervening":
            return (-candidate.intervening, candidate.pickup_cost, candidate.card)
        if self.candidate_ranking == "cheapest_pickup":
            return (candidate.pickup_cost, candidate.card)
        return (-candidate.card,)

    def __getattr__(self, name: str) -> Any:
        # Preserve optional tracing, statistics, and transactional-memory hooks.
        fallback = self.__dict__.get("fallback")
        if fallback is None:
            raise AttributeError(name)
        return getattr(fallback, name)


def build_count_threshold_bait(
    seed: int,
    *,
    intervening_card_threshold: int,
    candidate_ranking: CandidateRanking,
    fallback_strategy: str,
    fallback_options: dict[str, JsonValue] | None = None,
) -> CountThresholdBaitBot:
    from sixnimmt.arena.players import PlayerConfig, resolve_players

    player = PlayerConfig(bot=fallback_strategy, options=fallback_options if fallback_options is not None else {})
    fallback = resolve_players([player])[0].build(seed)
    return CountThresholdBaitBot(intervening_card_threshold, candidate_ranking, fallback)
