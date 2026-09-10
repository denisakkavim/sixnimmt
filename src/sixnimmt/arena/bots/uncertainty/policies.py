"""Exact card kernels for the supported board-and-hand policy catalogue."""

import random

from sixnimmt.arena.bots.base import Bot
from sixnimmt.arena.bots.closest_gap import ClosestGapBot
from sixnimmt.arena.bots.coldest_row import ColdestRowBot
from sixnimmt.arena.bots.hand_flexibility import HandFlexibilityBot
from sixnimmt.arena.bots.highest_card import HighestCardBot
from sixnimmt.arena.bots.highest_fitting_card import HighestFittingCardBot
from sixnimmt.arena.bots.lowest_card import LowestCardBot
from sixnimmt.arena.bots.lowest_fitting_card import LowestFittingCardBot
from sixnimmt.engine.actions import SelectCardAction
from sixnimmt.engine.state import Phase
from sixnimmt.engine.views import MatchView, PlayerSelfView, RowView

from .options import PolicyName

_POLICIES: dict[str, Bot] = {
    "lowest_card": LowestCardBot(),
    "highest_card": HighestCardBot(),
    "closest_gap": ClosestGapBot(),
    "coldest_row": ColdestRowBot(),
    "lowest_fitting_card": LowestFittingCardBot(),
    "highest_fitting_card": HighestFittingCardBot(),
    "hand_flexibility": HandFlexibilityBot(),
}


def observation(rows: tuple[RowView, ...], hand: tuple[int, ...]) -> MatchView:
    # These policies use only their own hand and the public board.
    return MatchView(
        match_id="simulation",
        view_version=0,
        view_id="simulation",
        status="active",
        phase=Phase.SELECTING,
        hand_number=1,
        play_number=1,
        you=PlayerSelfView(player_id="self", hand=hand),
        rows=rows,
        legal_actions=("select_card",),
    )


def preferred_card(policy: PolicyName, rows: tuple[RowView, ...], hand: tuple[int, ...]) -> int:
    action = _POLICIES[policy].act(observation(rows, hand))
    if not isinstance(action, SelectCardAction):
        msg = "card policy did not select a card"
        raise TypeError(msg)
    return action.card


def probability(policy: PolicyName, rows: tuple[RowView, ...], hand: tuple[int, ...], card: int) -> float:
    if card not in hand:
        return 0.0
    if policy == "random":
        return 1 / len(hand)
    return float(preferred_card(policy, rows, hand) == card)


def choose_card(
    policy: PolicyName, epsilon: float, rows: tuple[RowView, ...], hand: tuple[int, ...], rng: random.Random
) -> int:
    if policy == "random" or rng.random() < epsilon:
        return rng.choice(hand)
    return preferred_card(policy, rows, hand)
