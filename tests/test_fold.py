"""Views folded from a viewer's own event stream: correct, and free of hidden data."""

import pytest

from sixnimmt_server.engine.actions import ChooseRowAction, CommitAction, SelectCardAction, UncommitAction
from sixnimmt_server.engine.audience import Viewer
from sixnimmt_server.engine.cards import bull_heads
from sixnimmt_server.engine.events import Event, MatchAbandonedEvent
from sixnimmt_server.engine.fold import build_view
from sixnimmt_server.engine.rules import GameRules, MatchProtocol
from sixnimmt_server.engine.setup import create_match, open_match, start_match
from sixnimmt_server.engine.state import MatchState, Phase, PlayerSeat
from sixnimmt_server.engine.transition import transition
from sixnimmt_server.engine.views import MatchView, ViewRole

PLAYERS = ["alice", "bob", "cara"]
SPECTATOR = Viewer(role=ViewRole.PUBLIC_SPECTATOR)
OMNISCIENT = Viewer(role=ViewRole.OMNISCIENT_OBSERVER)


def _player(player_id: str) -> Viewer:
    return Viewer(role=ViewRole.PLAYER, player_id=player_id)


def _step(state: MatchState, events: list[Event]) -> MatchState:
    """Advance one decision, appending everything it emits."""
    if state.phase == Phase.AWAITING_ROW_CHOICE:
        assert state.resolution is not None and state.resolution.awaiting_player is not None
        actor, action = state.resolution.awaiting_player, ChooseRowAction(row_index=0)
    else:
        player = next(player for player in state.players if not player.committed)
        actor, action = player.player_id, SelectCardAction(card=player.hand[0])
    state, produced = transition(state, actor, action, MatchProtocol(), GameRules())
    events.extend(produced)
    return state


def _played_match(player_ids: list[str] = PLAYERS, seed: int = 12345) -> list[tuple[MatchState, list[Event]]]:
    """Every (live state, events so far) checkpoint of a complete match."""
    state, events = create_match("m_01", player_ids, match_seed=seed)
    log = list(events)
    checkpoints = [(state, list(log))]
    while state.phase != Phase.FINISHED:
        state = _step(state, log)
        checkpoints.append((state, list(log)))
    return checkpoints


def test_folded_view_tracks_live_state_at_every_point_of_a_match() -> None:
    for state, log in _played_match():
        for player in state.players:
            view = build_view(log, _player(player.player_id))

            assert view.hand_number == state.hand_number
            assert view.you.hand == player.hand
            assert view.you.penalty_cards == player.penalty_cards
            assert view.you.score_this_hand == player.score_this_hand
            assert view.you.total_score == player.total_score
            assert [row.cards for row in view.rows] == [row.cards for row in state.rows]


def _cards_in(view: MatchView) -> set[int]:
    """Only the fields that actually carry card numbers.

    Scraping every integer would flag scores and counts as if they were cards.
    """
    cards = set(view.you.hand) | set(view.you.penalty_cards)
    if view.you.selection is not None:
        cards.add(view.you.selection)
    cards.update(card for row in view.rows for card in row.cards)
    cards.update(card for opponent in view.players for card in opponent.penalty_cards)
    cards.update(card for revealed in view.revealed_this_hand for card in revealed)
    return cards


def test_no_view_ever_contains_another_players_hand_card() -> None:
    for state, log in _played_match():
        for player in state.players:
            hidden = {card for other in state.players if other.player_id != player.player_id for card in other.hand}

            assert _cards_in(build_view(log, _player(player.player_id))).isdisjoint(hidden)


def test_undealt_remainder_never_appears_in_any_view() -> None:
    for state, log in _played_match(["alice", "bob"]):
        remainder = set(state.undealt_remainder)
        for viewer in (_player("alice"), _player("bob"), SPECTATOR, OMNISCIENT):
            assert _cards_in(build_view(log, viewer)).isdisjoint(remainder)


def test_spectator_sees_the_table_but_no_hand_and_no_own_seat() -> None:
    state, log = _played_match()[-1][0], _played_match()[-1][1]

    view = build_view(log, SPECTATOR)

    assert view.you.hand == ()
    assert view.you.player_id == ""
    assert {opponent.player_id for opponent in view.players} == set(PLAYERS)
    assert view.legal_actions == ()
    assert [row.cards for row in view.rows] == [row.cards for row in state.rows]


def test_opponents_never_carry_a_selection_card() -> None:
    state, events = create_match("m_01", PLAYERS, match_seed=12345)
    log = list(events)
    state = _step(state, log)

    view = build_view(log, _player("bob"))

    alice = next(opponent for opponent in view.players if opponent.player_id == "alice")
    assert alice.has_selection is True
    assert alice.selection is None
    assert view.you.selection is None


def test_a_players_own_selection_is_visible_only_to_them() -> None:
    state, events = create_match("m_01", PLAYERS, match_seed=12345)
    log = list(events)
    card = state.players[0].hand[0]
    _step(state, log)

    assert build_view(log, _player("alice")).you.selection == card
    for viewer in (_player("bob"), SPECTATOR):
        view = build_view(log, viewer)
        assert view.you.selection is None
        assert all(opponent.selection is None for opponent in view.players)
        assert card not in _cards_in(view)


def test_view_version_is_the_viewers_own_gap_free_cursor() -> None:
    for _, log in _played_match():
        for player_id in PLAYERS:
            view = build_view(log, _player(player_id))
            assert view.view_version >= 0
    final_log = _played_match()[-1][1]
    versions = {player_id: build_view(final_log, _player(player_id)).view_version for player_id in PLAYERS}
    assert len(set(versions.values())) == 1, "classic play gives every player the same visible count"


def test_two_players_seeing_the_same_event_get_different_view_ids() -> None:
    log = _played_match()[-1][1]

    identifiers = {build_view(log, _player(player_id)).view_id for player_id in PLAYERS}

    assert len(identifiers) == len(PLAYERS)


def test_view_id_is_not_derived_from_global_activity() -> None:
    checkpoints = _played_match()
    early = build_view(checkpoints[2][1], _player("alice")).view_id
    late = build_view(checkpoints[-1][1], _player("alice")).view_id

    assert early != late
    assert not early.strip("v_").isdigit()
    assert not late.strip("v_").isdigit()


def test_no_global_counter_appears_in_a_player_view() -> None:
    log = _played_match()[-1][1]

    serialised = build_view(log, _player("alice")).model_dump()

    for forbidden in ("seq", "server_action_seq", "global_version", "mutation_version"):
        assert forbidden not in serialised


def test_legal_actions_offer_select_card_while_selecting() -> None:
    state, events = create_match("m_01", PLAYERS, match_seed=12345)

    view = build_view(events, _player("alice"))

    assert state.phase == Phase.SELECTING
    assert view.legal_actions == ("select_card",)


def test_only_the_awaited_player_may_choose_a_row() -> None:
    state, events = create_match("m_01", PLAYERS, match_seed=12345)
    log = list(events)
    while state.phase != Phase.AWAITING_ROW_CHOICE:
        state = _step(state, log)
        if state.phase == Phase.FINISHED:
            pytest.skip("no row choice arose in this match")

    assert state.resolution is not None
    awaited = state.resolution.awaiting_player
    assert awaited is not None
    for player_id in PLAYERS:
        view = build_view(log, _player(player_id))
        assert view.awaiting == awaited
        expected = ("choose_row",) if player_id == awaited else ()
        assert view.legal_actions == expected


def test_negotiation_offers_commit_and_uncommit_in_the_right_order() -> None:
    protocol = MatchProtocol(negotiation_enabled=True)
    state, events = create_match("m_01", PLAYERS, match_seed=12345, protocol=protocol)
    log = list(events)

    assert build_view(log, _player("alice")).legal_actions == ("select_card", "send_message")

    state, produced = transition(state, "alice", SelectCardAction(card=state.players[0].hand[0]), protocol, GameRules())
    log.extend(produced)
    assert build_view(log, _player("alice")).legal_actions == ("select_card", "commit", "send_message")


def test_setup_phase_is_visible_before_a_match_starts() -> None:
    state, events = open_match("m_01", PLAYERS, match_seed=12345)

    view = build_view(events, _player("alice"))

    assert state.phase == Phase.SETUP
    assert view.status == "pending"
    assert view.you.hand == ()
    assert view.legal_actions == ()

    started, start_events = start_match(state)
    running = build_view([*events, *start_events], _player("alice"))
    assert running.status == "in_progress"
    assert len(running.you.hand) == 10
    assert started.phase == Phase.SELECTING


def test_match_ended_view_reports_finished_status_and_final_totals() -> None:
    state, log = _played_match()[-1]

    view = build_view(log, _player("alice"))

    assert view.status == "finished"
    assert view.phase == Phase.FINISHED
    assert view.you.total_score == state.players[0].total_score
    assert view.legal_actions == ()


def test_scores_folded_from_events_match_the_heads_actually_captured() -> None:
    for state, log in _played_match(["alice", "bob"]):
        view = build_view(log, _player("alice"))
        assert view.you.score_this_hand == sum(bull_heads(card) for card in view.you.penalty_cards)
        assert view.you.score_this_hand == state.players[0].score_this_hand


def test_uncommitting_clears_the_public_commitment_in_every_view() -> None:
    protocol = MatchProtocol(negotiation_enabled=True)
    state, events = create_match("m_01", PLAYERS, match_seed=12345, protocol=protocol)
    log = list(events)
    state, produced = transition(state, "alice", SelectCardAction(card=state.players[0].hand[0]), protocol, GameRules())
    log.extend(produced)
    state, produced = transition(state, "alice", CommitAction(), protocol, GameRules())
    log.extend(produced)

    committed = build_view(log, _player("bob"))
    alice = next(opponent for opponent in committed.players if opponent.player_id == "alice")
    assert alice.committed is True

    state, produced = transition(state, "alice", UncommitAction(), protocol, GameRules())
    log.extend(produced)

    after = build_view(log, _player("bob"))
    alice_after = next(opponent for opponent in after.players if opponent.player_id == "alice")
    assert alice_after.committed is False
    assert alice_after.has_selection is False
    assert build_view(log, _player("alice")).legal_actions == ("select_card", "send_message")


def test_uncommit_is_offered_only_while_someone_else_is_uncommitted() -> None:
    protocol = MatchProtocol(negotiation_enabled=True)
    state, events = create_match("m_01", PLAYERS, match_seed=12345, protocol=protocol)
    log = list(events)
    for player in state.players[:2]:
        state, produced = transition(
            state, player.player_id, SelectCardAction(card=player.hand[0]), protocol, GameRules()
        )
        log.extend(produced)
        state, produced = transition(state, player.player_id, CommitAction(), protocol, GameRules())
        log.extend(produced)

    assert "uncommit" in build_view(log, _player("alice")).legal_actions


def test_an_action_budget_is_reported_as_remaining_actions() -> None:
    protocol = MatchProtocol(negotiation_enabled=True, max_actions_per_play=3)
    state, events = create_match("m_01", PLAYERS, match_seed=12345, protocol=protocol)
    log = list(events)

    assert build_view(log, _player("alice")).you.actions_remaining_this_play == 3

    _, produced = transition(state, "alice", SelectCardAction(card=state.players[0].hand[0]), protocol, GameRules())
    log.extend(produced)

    view = build_view(log, _player("alice"))
    assert view.you.actions_taken_this_play == 1
    assert view.you.actions_remaining_this_play == 2


def test_an_abandoned_match_folds_to_an_abandoned_status() -> None:
    state, events = create_match("m_01", PLAYERS, match_seed=12345)
    log = [*events, MatchAbandonedEvent(match_id=state.match_id, audience="public", data={})]

    view = build_view(log, _player("alice"))

    assert view.status == "abandoned"
    assert view.phase == Phase.FINISHED
    assert view.legal_actions == ()


def _named_match(anonymise: bool) -> list[Event]:
    """A started match whose players carry display names distinct from their ids."""
    seats = [
        PlayerSeat(player_id="alice", display_name="Ada"),
        PlayerSeat(player_id="bob", display_name="Grace"),
        PlayerSeat(player_id="cara", display_name="Edsger"),
    ]
    _, events = create_match(
        "m_01",
        seats,
        match_seed=12345,
        protocol=MatchProtocol(anonymise_display_names=anonymise),
    )
    return events


def test_display_names_are_shown_when_anonymising_is_off() -> None:
    view = build_view(_named_match(anonymise=False), _player("alice"))

    assert [opponent.display_name for opponent in view.players] == ["Grace", "Edsger"]


@pytest.mark.parametrize(
    "viewer",
    [pytest.param(_player("alice"), id="player"), pytest.param(SPECTATOR, id="public_spectator")],
)
def test_anonymising_replaces_opponent_names_with_seat_numbers(viewer: Viewer) -> None:
    view = build_view(_named_match(anonymise=True), viewer)

    assert [(opponent.player_id, opponent.display_name) for opponent in view.players] == [
        (player_id, f"Player {seat}")
        for player_id, seat in [("alice", 1), ("bob", 2), ("cara", 3)]
        if player_id != viewer.player_id
    ]


def test_omniscient_observers_keep_real_names_while_anonymising() -> None:
    view = build_view(_named_match(anonymise=True), OMNISCIENT)

    assert [opponent.display_name for opponent in view.players] == ["Ada", "Grace", "Edsger"]
