"""Sampled continuations resolved by the engine without dealing new hands."""

import math
import random

from sixnimmt.arena.bots._board import cheapest_row
from sixnimmt.arena.bots.hand_aware_row_choice import HandAwareRowChoiceBot
from sixnimmt.arena.bots.highest_card import HighestCardBot
from sixnimmt.engine.actions import ChooseRowAction
from sixnimmt.engine.resolution import advance_resolution, choose_row
from sixnimmt.engine.state import MatchState, Phase, PlayerState, ResolutionState, RowState
from sixnimmt.engine.views import MatchView, RowView

from .inference import World
from .options import PenaltyObjective, RowPolicyOptions, SimulationOptions
from .policies import choose_card, observation


def select_row(rows: tuple[RowView, ...], hand: tuple[int, ...], card: int, policy: RowPolicyOptions) -> int:
    if policy.policy == "cheapest":
        return cheapest_row(rows).index
    view = observation(rows, hand).model_copy(update={"legal_actions": ("choose_row",), "awaiting_card": card})
    if policy.max_extra_penalty is None:
        msg = "hand-aware row choice requires max_extra_penalty"
        raise ValueError(msg)
    bot = HandAwareRowChoiceBot(policy.max_extra_penalty, HighestCardBot())
    action = bot.act(view)
    if not isinstance(action, ChooseRowAction):
        msg = "row policy did not choose a row"
        raise TypeError(msg)
    return action.row_index


def initial_state(view: MatchView, world: World) -> MatchState:
    hands = iter(world.hands)
    players = [
        PlayerState(
            player_id=view.you.player_id,
            hand=view.you.hand,
            penalty_cards=view.you.penalty_cards,
            score_this_hand=view.you.score_this_hand,
            total_score=view.you.total_score,
        )
    ]
    for player in view.players:
        hand = view.you.hand if player.player_id == view.you.player_id else next(hands)
        players.append(
            PlayerState(
                player_id=player.player_id,
                hand=hand,
                penalty_cards=player.penalty_cards,
                score_this_hand=player.score_this_hand,
                total_score=player.total_score,
            )
        )
    return MatchState(
        match_id="simulation",
        phase=Phase.SELECTING,
        players=tuple(players),
        rows=tuple(RowState(index=row.index, cards=row.cards) for row in view.rows),
        hand_number=view.hand_number,
        play_number=view.play_number,
        undealt_remainder=world.undealt,
    )


def resolve_turn(
    state: MatchState, selections: dict[str, int], own_id: str, row_policy: RowPolicyOptions
) -> MatchState:
    players = tuple(
        player.model_copy(update={"hand": tuple(card for card in player.hand if card != selections[player.player_id])})
        for player in state.players
    )
    ordered = tuple(sorted((card, player_id) for player_id, card in selections.items()))
    state = state.model_copy(
        update={
            "players": players,
            "phase": Phase.RESOLVING,
            "resolution": ResolutionState(
                ordered_cards=ordered, scores_before_play=tuple((p.player_id, p.score_this_hand) for p in state.players)
            ),
        }
    )
    while state.resolution is not None and state.resolution.next_index < len(ordered):
        if state.phase == Phase.AWAITING_ROW_CHOICE:
            card, player_id = state.resolution.ordered_cards[state.resolution.next_index]
            player = next(player for player in state.players if player.player_id == player_id)
            rows = tuple(RowView(index=row.index, cards=row.cards) for row in state.rows)
            index = select_row(rows, player.hand, card, row_policy) if player_id == own_id else cheapest_row(rows).index
            state, _ = choose_row(state, player_id, index)
        else:
            state, _ = advance_resolution(state)
    return state.model_copy(update={"phase": Phase.SELECTING, "resolution": None, "play_number": state.play_number + 1})


def rollout(view: MatchView, world: World, candidate: int, options: SimulationOptions, seed: int) -> int:
    rng = random.Random(seed)  # noqa: S311 -- seeded experimental simulation
    state = initial_state(view, world)
    turns = len(view.you.hand) if options.horizon == "remaining_hand" else min(options.horizon, len(view.you.hand))
    for turn in range(turns):
        rows = tuple(RowView(index=row.index, cards=row.cards) for row in state.rows)
        selections = {}
        opponent_index = 0
        for player in state.players:
            if player.player_id == view.you.player_id:
                card = candidate if turn == 0 else choose_card(options.continuation_policy, 0, rows, player.hand, rng)
            else:
                policy = options.model.policies[world.policies[opponent_index]]
                card = choose_card(policy, world.epsilons[opponent_index], rows, player.hand, rng)
                opponent_index += 1
            selections[player.player_id] = card
        state = resolve_turn(state, selections, view.you.player_id, options.row_policy)
    own = next(player for player in state.players if player.player_id == view.you.player_id)
    return own.score_this_hand - view.you.score_this_hand


def penalty_value(samples: list[int], objective: PenaltyObjective) -> float:
    if not samples:
        msg = "penalty evaluation requires samples"
        raise ValueError(msg)
    if objective.kind == "mean":
        return sum(samples) / len(samples)
    if objective.kind == "pickup_probability":
        return sum(value > 0 for value in samples) / len(samples)
    if objective.kind == "threshold_exceedance" and objective.threshold is not None:
        return sum(value > objective.threshold for value in samples) / len(samples)
    if objective.tail_fraction is None:
        msg = "upper-tail evaluation requires tail_fraction"
        raise ValueError(msg)
    mass = objective.tail_fraction * len(samples)
    ordered = sorted(samples, reverse=True)
    complete = math.floor(mass)
    total = float(sum(ordered[:complete]))
    if complete < len(ordered):
        total += (mass - complete) * ordered[complete]
    return total / mass
