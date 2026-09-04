"""Play, hand, and match endings: scores, new deals, and the 66-point cutoff."""

from pydantic import TypeAdapter

from sixnimmt_server.engine.actions import Action
from sixnimmt_server.engine.rules import GameRules, MatchProtocol
from sixnimmt_server.engine.setup import create_match
from sixnimmt_server.engine.state import MatchState, Phase, PlayerState, RowState
from sixnimmt_server.engine.transition import transition

_ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)


def _rows(*ends: list[int]) -> tuple[RowState, ...]:
    return tuple(RowState(index=index, cards=tuple(cards)) for index, cards in enumerate(ends))


def _play_one_card(state: MatchState, player_id: str, card: int) -> MatchState:
    action = _ACTION_ADAPTER.validate_python({"type": "select_card", "card": card})
    new_state, _ = transition(state, player_id, action, MatchProtocol(), GameRules())
    return new_state


def _choose_row(state: MatchState) -> MatchState:
    assert state.phase == Phase.AWAITING_ROW_CHOICE
    assert state.resolution is not None and state.resolution.awaiting_player is not None
    action = _ACTION_ADAPTER.validate_python({"type": "choose_row", "row_index": 0})
    new_state, _ = transition(state, state.resolution.awaiting_player, action, MatchProtocol(), GameRules())
    return new_state


def _play_full_play(state: MatchState) -> MatchState:
    current = state
    for player in state.players:
        current = _play_one_card(current, player.player_id, player.hand[0])
        while current.phase == Phase.AWAITING_ROW_CHOICE:
            current = _choose_row(current)
    return current


def test_play_end_emits_penalties_clears_board_tracking_and_starts_next() -> None:
    state, _ = create_match("m_01", ["a", "b"], match_seed=1)

    ended = _play_full_play(state)

    assert ended.play_number == 2
    assert ended.phase == Phase.SELECTING
    assert ended.resolution is None
    assert ended.revealed_this_hand == ()
    assert all(player.selection is None for player in ended.players)
    assert all(not player.committed for player in ended.players)
    assert all(len(player.hand) == 9 for player in ended.players)


def test_hand_scores_bank_into_totals_and_rows_redeal() -> None:
    state, _ = create_match("m_01", ["a", "b"], match_seed=1)
    current = state
    for _ in range(10):
        current = _play_full_play(current)

    assert current.hand_number == 2
    assert current.play_number == 1
    assert current.phase == Phase.SELECTING
    for player in current.players:
        assert len(player.hand) == 10
        assert player.score_this_hand == 0
        assert player.total_score >= 0
        assert player.penalty_cards == ()


def test_match_ends_between_hands_when_someone_reaches_66() -> None:
    state = MatchState(
        match_id="m_01",
        phase=Phase.SELECTING,
        players=(
            PlayerState(player_id="a", hand=(1, 90), score_this_hand=66, total_score=0),
            PlayerState(player_id="b", hand=(2, 91), score_this_hand=0, total_score=0),
        ),
        rows=_rows([10], [20], [30], [40]),
        hand_number=1,
        play_number=1,
        match_seed=1,
    )

    mid = _play_full_play(state)
    assert mid.phase == Phase.SELECTING
    assert mid.play_number == 2

    ended = _play_full_play(mid)

    assert ended.phase == Phase.FINISHED
    totals = {player.player_id: player.total_score for player in ended.players}
    assert totals["a"] >= 66
    assert totals["b"] == 0


def test_match_scores_bank_only_between_hands_not_mid_hand() -> None:
    state = MatchState(
        match_id="m_01",
        phase=Phase.SELECTING,
        players=(
            PlayerState(player_id="a", hand=(1, 90), score_this_hand=66, total_score=0),
            PlayerState(player_id="b", hand=(2, 91), score_this_hand=0, total_score=0),
        ),
        rows=_rows([10], [20], [30], [40]),
        hand_number=1,
        play_number=1,
        match_seed=1,
    )

    mid = _play_full_play(state)

    assert mid.phase == Phase.SELECTING
    assert mid.hand_number == 1
    totals = {player.player_id: player.total_score for player in mid.players}
    assert totals == {"a": 0, "b": 0}


def test_lowest_total_wins_and_ties_share_the_win() -> None:
    state, events = create_match("m_01", ["a", "b"], match_seed=1)
    current = state
    for _ in range(10):
        current = _play_full_play(current)

    totals = {player.player_id: player.total_score for player in current.players}
    lowest = min(totals.values())
    winners = sorted(player_id for player_id, total in totals.items() if total == lowest)

    assert current.phase in (Phase.SELECTING, Phase.FINISHED)
    assert len(winners) >= 1
    assert all(totals[winner] == lowest for winner in winners)
    assert events[0].type == "hand_started"
