"""Play, hand, and match endings: per-play penalties, banking, and the cutoff."""

from sixnimmt_server.engine.events import (
    Event,
    HandEndedEvent,
    MatchEndedEvent,
    PlayEndedEvent,
    PlayStartedEvent,
)
from sixnimmt_server.engine.rules import EndCondition, GameRules, MatchProtocol
from sixnimmt_server.engine.setup import start_hand
from sixnimmt_server.engine.state import MatchState, Phase


def _play_penalties(state: MatchState) -> dict[str, int]:
    if state.resolution is None:
        msg = "cannot close a play without resolution state"
        raise ValueError(msg)
    before_scores = dict(state.resolution.scores_before_play)
    return {player.player_id: player.score_this_hand - before_scores[player.player_id] for player in state.players}


def _reset_play_state(state: MatchState) -> tuple:
    return tuple(
        player.model_copy(
            update={
                "selection": None,
                "committed": False,
                "actions_taken_this_play": 0,
            }
        )
        for player in state.players
    )


def _bank_hand_scores(state: MatchState) -> MatchState:
    players = tuple(
        player.model_copy(
            update={
                "total_score": player.total_score + player.score_this_hand,
                "score_this_hand": 0,
                "penalty_cards": (),
                "selection": None,
                "committed": False,
                "actions_taken_this_play": 0,
            }
        )
        for player in state.players
    )
    return state.model_copy(update={"players": players, "resolution": None})


def _should_end_match(state: MatchState, rules: GameRules, protocol: MatchProtocol) -> bool:
    if protocol.end_condition == EndCondition.FIXED_HANDS:
        if protocol.hands is None:
            msg = "fixed-hands protocol requires a hand count"
            raise ValueError(msg)
        return state.hand_number >= protocol.hands
    return any(player.total_score >= rules.target_score for player in state.players)


def _end_hand(
    state: MatchState,
    rules: GameRules,
    protocol: MatchProtocol,
) -> tuple[MatchState, list[Event]]:
    hand_scores = {player.player_id: player.score_this_hand for player in state.players}
    banked = _bank_hand_scores(state)
    totals = {player.player_id: player.total_score for player in banked.players}
    ended: Event = HandEndedEvent(
        match_id=state.match_id,
        hand=state.hand_number,
        play=state.play_number,
        audience="public",
        data={"hand": state.hand_number, "hand_scores": hand_scores, "totals": totals},
    )

    if _should_end_match(banked, rules, protocol):
        lowest = min(totals.values())
        winners = sorted(player_id for player_id, total in totals.items() if total == lowest)
        finished = banked.model_copy(update={"phase": Phase.FINISHED})
        match_ended: Event = MatchEndedEvent(
            match_id=state.match_id,
            hand=state.hand_number,
            play=state.play_number,
            audience="public",
            data={"totals": totals, "winners": winners},
        )
        return finished, [ended, match_ended]

    next_hand, start_events = start_hand(banked, state.hand_number + 1)
    return next_hand, [ended, *start_events]


def end_play(
    state: MatchState,
    rules: GameRules,
    protocol: MatchProtocol,
) -> tuple[MatchState, list[Event]]:
    """Close a fully resolved play and advance to the next match boundary."""
    penalties = _play_penalties(state)
    ended: Event = PlayEndedEvent(
        match_id=state.match_id,
        hand=state.hand_number,
        play=state.play_number,
        audience="public",
        data={"play": state.play_number, "penalties": penalties},
    )

    hand_is_complete = state.play_number >= rules.cards_per_hand or all(
        len(player.hand) == 0 for player in state.players
    )
    if hand_is_complete:
        next_state, boundary_events = _end_hand(state, rules, protocol)
        return next_state, [ended, *boundary_events]

    next_play_number = state.play_number + 1
    next_play = state.model_copy(
        update={
            "phase": Phase.SELECTING,
            "play_number": next_play_number,
            "players": _reset_play_state(state),
            "resolution": None,
        }
    )
    started: Event = PlayStartedEvent(
        match_id=state.match_id,
        hand=state.hand_number,
        play=next_play_number,
        audience="public",
        data={"hand": state.hand_number, "play": next_play_number},
    )
    return next_play, [ended, started]
