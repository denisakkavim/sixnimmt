"""Board invariants held after every placement."""

from pydantic import TypeAdapter

from sixnimmt_server.engine.actions import Action
from sixnimmt_server.engine.cards import bull_heads, full_deck
from sixnimmt_server.engine.rules import GameRules, MatchProtocol
from sixnimmt_server.engine.setup import create_match
from sixnimmt_server.engine.state import MatchState, Phase
from sixnimmt_server.engine.transition import transition

_ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)


def _all_card_locations(state: MatchState) -> list[int]:
    locations: list[int] = []
    for player in state.players:
        locations.extend(player.hand)
        locations.extend(player.penalty_cards)
    for row in state.rows:
        locations.extend(row.cards)
    locations.extend(state.undealt_remainder)
    if state.resolution is not None:
        locations.extend(card for card, _ in state.resolution.ordered_cards[state.resolution.next_index :])
    return locations


def _check_board(state: MatchState) -> None:
    for row in state.rows:
        assert 1 <= len(row.cards) <= 5
        pairs = zip(row.cards, row.cards[1:], strict=False)
        assert all(earlier < later for earlier, later in pairs)
    locations = _all_card_locations(state)
    assert len(locations) == len(set(locations))


def _step(state: MatchState) -> MatchState:
    for player in state.players:
        if state.phase == Phase.FINISHED:
            return state
        if state.phase != Phase.SELECTING or len(player.hand) == 0:
            continue
        card = next(card for card in player.hand)
        action = _ACTION_ADAPTER.validate_python({"type": "select_card", "card": card})
        state, _ = transition(state, player.player_id, action, MatchProtocol(), GameRules())
        while state.phase == Phase.AWAITING_ROW_CHOICE:
            assert state.resolution is not None and state.resolution.awaiting_player is not None
            choice = _ACTION_ADAPTER.validate_python({"type": "choose_row", "row_index": 0})
            state, _ = transition(state, state.resolution.awaiting_player, choice, MatchProtocol(), GameRules())
        _check_board(state)
    return state


def test_every_card_in_exactly_one_place_after_each_step() -> None:
    state, _ = create_match("m_01", ["a", "b", "c"], match_seed=12345)
    _check_board(state)
    steps = 0

    for _ in range(30):
        state = _step(state)
        steps += 1
        if state.phase == Phase.FINISHED:
            break

    assert steps > 0


def test_penalty_heads_never_exceed_171() -> None:
    state, _ = create_match("m_01", ["a", "b"], match_seed=7)

    for _ in range(20):
        state = _step(state)
        total = sum(bull_heads(card) for player in state.players for card in player.penalty_cards)
        assert 0 <= total <= 171
        if state.phase == Phase.FINISHED:
            break


def test_deck_cards_are_never_lost_or_duplicated() -> None:
    state, _ = create_match("m_01", ["a", "b"], match_seed=42)
    deck = set(full_deck())

    for _ in range(20):
        state = _step(state)
        assert set(_all_card_locations(state)) <= deck
        if state.phase == Phase.FINISHED:
            break
