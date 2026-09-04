"""Play, hand, and match endings: per-play penalties, banking, and the cutoff."""

from sixnimmt_server.engine.cards import bull_heads
from sixnimmt_server.engine.events import (
    Event,
    HandEndedEvent,
    HandStartedEvent,
    MatchEndedEvent,
    PlayEndedEvent,
    PlayStartedEvent,
)
from sixnimmt_server.engine.setup import start_hand
from sixnimmt_server.engine.state import MatchState, Phase


def _play_penalties(before: MatchState, after: MatchState, cards: tuple[tuple[int, str], ...]) -> dict[str, int]:
    before_scores = {player.player_id: player.score_this_hand for player in before.players}
    after_scores = {player.player_id: player.score_this_hand for player in after.players}
    penalties = {player_id: after_scores[player_id] - before_scores[player_id] for player_id in after_scores}
    cards_by_player = {player_id: card for card, player_id in cards}
    for player_id, penalty in penalties.items():
        card = cards_by_player[player_id]
        heads = bull_heads(card)
        if penalty < 0 or penalty > heads + 35:
            msg = f"penalty {penalty} for card {card} is outside 0 to {heads + 35}"
            raise ValueError(msg)
    return penalties


def end_play(
    before: MatchState, after: MatchState, ordered_cards: tuple[tuple[int, str], ...]
) -> tuple[MatchState, list[Event]]:
    """Close a fully resolved play and start the next one."""
    penalties = _play_penalties(before, after, ordered_cards)
    cleared = after.model_copy(
        update={
            "phase": Phase.SELECTING,
            "play_number": after.play_number + 1,
            "resolution": None,
            "revealed_this_hand": (),
        }
    )
    event: Event = PlayEndedEvent(
        match_id=after.match_id,
        hand=after.hand_number,
        play=after.play_number,
        audience="public",
        data={"play": after.play_number, "penalties": penalties},
    )
    started: Event = PlayStartedEvent(
        match_id=after.match_id,
        hand=after.hand_number,
        play=after.play_number + 1,
        audience="public",
        data={"hand": after.hand_number, "play": after.play_number + 1},
    )
    return cleared, [event, started]


def end_hand(state: MatchState, target_score: int) -> tuple[MatchState, list[Event]]:
    """Bank hand scores into totals; end the match or deal the next hand."""
    banked_players = tuple(
        player.model_copy(
            update={
                "total_score": player.total_score + player.score_this_hand,
                "score_this_hand": 0,
                "penalty_cards": (),
            }
        )
        for player in state.players
    )
    banked = state.model_copy(update={"players": banked_players})
    totals = {player.player_id: player.total_score for player in banked.players}
    hand_scores = {player.player_id: player.score_this_hand for player in state.players}
    ended_event: Event = HandEndedEvent(
        match_id=state.match_id,
        hand=state.hand_number,
        play=state.play_number,
        audience="public",
        data={"hand": state.hand_number, "hand_scores": hand_scores, "totals": totals},
    )
    if any(total >= target_score for total in totals.values()):
        lowest = min(totals.values())
        winners = sorted(player_id for player_id, total in totals.items() if total == lowest)
        finished = banked.model_copy(update={"phase": Phase.FINISHED})
        match_event: Event = MatchEndedEvent(
            match_id=state.match_id,
            hand=state.hand_number,
            play=state.play_number,
            audience="public",
            data={"totals": totals, "winners": winners},
        )
        return finished, [ended_event, match_event]
    next_hand_number = state.hand_number + 1
    dealt, deal_events = start_hand(banked, next_hand_number)
    hand_started: Event = HandStartedEvent(
        match_id=state.match_id,
        hand=next_hand_number,
        play=1,
        audience="public",
        data={"hand_number": next_hand_number},
    )
    return dealt, [ended_event, hand_started, *deal_events]
