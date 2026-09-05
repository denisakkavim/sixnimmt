"""Match setup: open a match, start it, and deal each hand from its seed."""

from collections.abc import Sequence

from sixnimmt_server.engine.cards import deal, hand_seed_for, shuffled_deck
from sixnimmt_server.engine.errors import EngineRejection, ErrorCode
from sixnimmt_server.engine.events import (
    PLAYER_ID_PATTERN,
    CardsDealtEvent,
    Event,
    HandSeedAssignedEvent,
    HandStartedEvent,
    MatchCreatedEvent,
    MatchSeedAssignedEvent,
    MatchStartedEvent,
    PlayStartedEvent,
    RowsInitialisedEvent,
    audience_for_player,
)
from sixnimmt_server.engine.rules import GameRules, MatchProtocol
from sixnimmt_server.engine.state import MatchState, Phase, PlayerSeat, PlayerState, RowState


def _seats(players: Sequence[str | PlayerSeat]) -> tuple[PlayerSeat, ...]:
    return tuple(PlayerSeat(player_id=player) if isinstance(player, str) else player for player in players)


def _check_seats(seats: tuple[PlayerSeat, ...], rules: GameRules) -> None:
    if not rules.min_players <= len(seats) <= rules.max_players:
        msg = f"a match needs between {rules.min_players} and {rules.max_players} players, got {len(seats)}"
        raise EngineRejection(ErrorCode.INVALID_PLAYER_COUNT, msg)
    # The grammar is checked before anything else quotes an id back to the
    # caller. An id has to survive an event audience, a token map key and a
    # UTF-8 log, and a lone surrogate is a legal JSON string that cannot be
    # encoded at all — including into the very refusal that names it.
    unusable = sorted({seat.player_id for seat in seats if not PLAYER_ID_PATTERN.match(seat.player_id)})
    if unusable:
        # `ascii` so that an id which cannot be encoded is still reportable.
        rejected = ", ".join(ascii(player_id) for player_id in unusable)
        msg = f"player ids must be 1-64 characters of letters, digits, '_' or '-', rejected: {rejected}"
        raise EngineRejection(ErrorCode.INVALID_PLAYER_ID, msg)
    identifiers = [seat.player_id for seat in seats]
    duplicates = sorted({name for name in identifiers if identifiers.count(name) > 1})
    if duplicates:
        msg = f"player ids must be unique, repeated: {', '.join(duplicates)}"
        raise EngineRejection(ErrorCode.DUPLICATE_PLAYER_ID, msg)


def open_match(
    match_id: str,
    players: Sequence[str | PlayerSeat],
    match_seed: int,
    rules: GameRules | None = None,
    protocol: MatchProtocol | None = None,
) -> tuple[MatchState, list[Event]]:
    """Register a match without dealing. The first hand arrives with start_match."""
    rules = rules or GameRules()
    protocol = protocol or MatchProtocol()
    seats = _seats(players)
    _check_seats(seats, rules)

    state = MatchState(
        match_id=match_id,
        phase=Phase.SETUP,
        players=tuple(
            PlayerState(
                player_id=seat.player_id,
                display_name=seat.display_name,
                agent_metadata=seat.agent_metadata,
            )
            for seat in seats
        ),
        match_seed=match_seed,
    )
    public_players = [{"player_id": seat.player_id, "display_name": seat.name_or_id} for seat in seats]
    shared = {"rules": rules.model_dump(mode="json"), "protocol": protocol.model_dump(mode="json")}
    events: list[Event] = [
        MatchCreatedEvent(
            match_id=match_id,
            hand=1,
            play=1,
            audience="public",
            data={**shared, "players": public_players},
        ),
        # Agent metadata must reach the log for later analysis but never another
        # player, so the admin copy carries it and the public one does not.
        MatchCreatedEvent(
            match_id=match_id,
            hand=1,
            play=1,
            audience="admin",
            data={**shared, "players": [seat.model_dump(mode="json") for seat in seats]},
        ),
        MatchSeedAssignedEvent(
            match_id=match_id,
            hand=1,
            play=1,
            audience="admin",
            data={"match_seed": match_seed},
        ),
    ]
    return state, events


def _deal_hand(
    state: MatchState,
    hand_number: int,
) -> tuple[MatchState, list[Event]]:
    if state.match_seed is None:
        msg = "cannot deal a hand without a match seed"
        raise ValueError(msg)
    match_id = state.match_id
    player_ids = [player.player_id for player in state.players]
    hands, row_starts, remainder = deal(shuffled_deck(state.match_seed, hand_number), player_ids)
    players = tuple(
        player.model_copy(
            update={
                "hand": tuple(hands[player.player_id]),
                "selection": None,
                "committed": False,
                "penalty_cards": (),
                "score_this_hand": 0,
                "actions_taken_this_play": 0,
            }
        )
        for player in state.players
    )
    dealt = MatchState(
        match_id=match_id,
        phase=Phase.SELECTING,
        players=players,
        rows=tuple(RowState(index=index, cards=(card,)) for index, card in enumerate(row_starts)),
        hand_number=hand_number,
        play_number=1,
        match_seed=state.match_seed,
        undealt_remainder=tuple(remainder),
    )
    events: list[Event] = [
        HandStartedEvent(
            match_id=match_id,
            hand=hand_number,
            play=1,
            audience="public",
            data={"hand_number": hand_number},
        ),
        HandSeedAssignedEvent(
            match_id=match_id,
            hand=hand_number,
            play=1,
            audience="admin",
            data={"hand_number": hand_number, "hand_seed": hand_seed_for(state.match_seed, hand_number)},
        ),
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
    return dealt, events


def start_match(state: MatchState) -> tuple[MatchState, list[Event]]:
    """Deal the first hand and open the first play."""
    if state.phase != Phase.SETUP:
        msg = f"cannot start a match during {state.phase.value}"
        raise EngineRejection(ErrorCode.WRONG_PHASE, msg)
    started: Event = MatchStartedEvent(
        match_id=state.match_id,
        hand=1,
        play=1,
        audience="public",
        data={},
    )
    dealt, deal_events = _deal_hand(state, hand_number=1)
    return dealt, [started, *deal_events]


def create_match(
    match_id: str,
    players: Sequence[str | PlayerSeat],
    match_seed: int,
    rules: GameRules | None = None,
    protocol: MatchProtocol | None = None,
) -> tuple[MatchState, list[Event]]:
    """Open a match and immediately start it, for callers that never pause at SETUP."""
    opened, opening_events = open_match(match_id, players, match_seed, rules, protocol)
    started, start_events = start_match(opened)
    return started, [*opening_events, *start_events]


def start_hand(state: MatchState, hand_number: int) -> tuple[MatchState, list[Event]]:
    return _deal_hand(state, hand_number)
