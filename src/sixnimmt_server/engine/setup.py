"""Match setup: create a match and deal each hand from its seed."""

from sixnimmt_server.engine.cards import deal, shuffled_deck
from sixnimmt_server.engine.events import (
    CardsDealtEvent,
    Event,
    HandStartedEvent,
    PlayStartedEvent,
    RowsInitialisedEvent,
    audience_for_player,
)
from sixnimmt_server.engine.state import MatchState, Phase, PlayerState, RowState


def _deal_hand(
    match_id: str,
    player_ids: list[str],
    match_seed: int,
    hand_number: int,
    previous: MatchState | None = None,
) -> tuple[MatchState, list[Event]]:
    hands, row_starts, remainder = deal(shuffled_deck(match_seed, hand_number), player_ids)
    if previous is None:
        players = tuple(PlayerState(player_id=player_id, hand=tuple(hands[player_id])) for player_id in player_ids)
    else:
        players = tuple(
            player.model_copy(update={"hand": tuple(hands[player.player_id])}) for player in previous.players
        )
    state = MatchState(
        match_id=match_id,
        phase=Phase.SELECTING,
        players=players,
        rows=tuple(RowState(index=index, cards=(card,)) for index, card in enumerate(row_starts)),
        hand_number=hand_number,
        play_number=1,
        match_seed=match_seed,
        undealt_remainder=tuple(remainder),
    )
    events: list[Event] = [
        HandStartedEvent(
            match_id=match_id,
            hand=hand_number,
            play=1,
            audience="public",
            data={"hand_number": hand_number},
        )
    ]
    events.extend(
        CardsDealtEvent(
            match_id=match_id,
            hand=hand_number,
            play=1,
            audience=audience_for_player(player_id),
            data={"player_id": player_id, "hand": hands[player_id]},
        )
        for player_id in player_ids
    )
    events.append(
        RowsInitialisedEvent(
            match_id=match_id,
            hand=hand_number,
            play=1,
            audience="public",
            data={"rows": [[card] for card in row_starts]},
        )
    )
    events.append(
        PlayStartedEvent(
            match_id=match_id,
            hand=hand_number,
            play=1,
            audience="public",
            data={"hand": hand_number, "play": 1},
        )
    )
    return state, events


def create_match(match_id: str, player_ids: list[str], match_seed: int) -> tuple[MatchState, list[Event]]:
    return _deal_hand(match_id, player_ids, match_seed, hand_number=1)


def start_hand(state: MatchState, hand_number: int) -> tuple[MatchState, list[Event]]:
    if state.match_seed is None:
        msg = "cannot deal next hand without a match seed"
        raise ValueError(msg)
    player_ids = [player.player_id for player in state.players]
    return _deal_hand(state.match_id, player_ids, state.match_seed, hand_number, previous=state)
