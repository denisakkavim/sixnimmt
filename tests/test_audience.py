"""Event visibility per role, and the gap-free per-viewer cursor built on it."""

import pytest

from sixnimmt_server.engine.actions import ChooseRowAction, SelectCardAction
from sixnimmt_server.engine.audience import (
    Viewer,
    events_since,
    view_version,
    visible_events,
    visible_to,
)
from sixnimmt_server.engine.events import Event, EventType, PlayStartedEvent
from sixnimmt_server.engine.rules import GameRules, MatchProtocol
from sixnimmt_server.engine.setup import create_match
from sixnimmt_server.engine.state import Phase
from sixnimmt_server.engine.transition import transition
from sixnimmt_server.engine.views import ViewRole

ALICE = Viewer(role=ViewRole.PLAYER, player_id="alice")
BOB = Viewer(role=ViewRole.PLAYER, player_id="bob")
SPECTATOR = Viewer(role=ViewRole.PUBLIC_SPECTATOR)
OMNISCIENT = Viewer(role=ViewRole.OMNISCIENT_OBSERVER)
ADMIN = Viewer(role=ViewRole.ADMIN)


def _event(audience: str) -> Event:
    return PlayStartedEvent(match_id="m_01", audience=audience, data={})


def _full_match_events(player_ids: list[str], match_seed: int = 12345) -> list[Event]:
    state, events = create_match("m_01", player_ids, match_seed=match_seed)
    collected = list(events)
    while state.phase != Phase.FINISHED:
        for player in state.players:
            if state.phase != Phase.SELECTING:
                continue
            action = SelectCardAction(card=player.hand[0])
            state, produced = transition(state, player.player_id, action, MatchProtocol(), GameRules())
            collected.extend(produced)
            while state.phase == Phase.AWAITING_ROW_CHOICE:
                assert state.resolution is not None and state.resolution.awaiting_player is not None
                state, produced = transition(
                    state,
                    state.resolution.awaiting_player,
                    ChooseRowAction(row_index=0),
                    MatchProtocol(),
                    GameRules(),
                )
                collected.extend(produced)
    return collected


def _serialise(events: list[Event], viewer: Viewer) -> str:
    """Every visible event as JSON, minus timestamps whose digits are incidental."""
    return " ".join(event.model_dump_json(exclude={"timestamp"}) for event in visible_events(events, viewer))


@pytest.mark.parametrize(
    ("viewer", "audience", "expected"),
    [
        (ALICE, "public", True),
        (ALICE, "player:alice", True),
        (ALICE, "player:bob", False),
        (ALICE, "admin", False),
        (SPECTATOR, "public", True),
        (SPECTATOR, "player:alice", False),
        (SPECTATOR, "admin", False),
        (OMNISCIENT, "public", True),
        (OMNISCIENT, "player:alice", True),
        (OMNISCIENT, "player:bob", True),
        (OMNISCIENT, "admin", False),
        (ADMIN, "public", True),
        (ADMIN, "player:alice", True),
        (ADMIN, "admin", True),
    ],
)
def test_audience_decides_who_receives_an_event(viewer: Viewer, audience: str, expected: bool) -> None:
    assert visible_to(_event(audience), viewer) is expected


def test_omniscient_observers_never_receive_admin_events() -> None:
    _, events = create_match("m_01", ["alice", "bob"], match_seed=12345)

    seen = visible_events(events, OMNISCIENT)

    assert all(event.audience != "admin" for event in seen)
    assert not any(event.type == EventType.MATCH_SEED_ASSIGNED for event in seen)
    assert not any(event.type == EventType.HAND_SEED_ASSIGNED for event in seen)


def test_no_seed_reaches_any_stream_below_admin() -> None:
    seed = 12345
    events = _full_match_events(["alice", "bob"], match_seed=seed)

    for viewer in (ALICE, BOB, SPECTATOR, OMNISCIENT):
        assert str(seed) not in _serialise(events, viewer)

    assert str(seed) in _serialise(events, ADMIN)


def test_a_player_never_sees_another_players_dealt_hand() -> None:
    events = _full_match_events(["alice", "bob"])

    dealt = [event for event in visible_events(events, ALICE) if event.type == EventType.CARDS_DEALT]

    assert dealt
    assert all(event.data["player_id"] == "alice" for event in dealt)


def test_each_players_cursor_is_contiguous_from_one_across_a_full_match() -> None:
    events = _full_match_events(["alice", "bob", "cara"])

    for viewer in (ALICE, BOB, SPECTATOR):
        numbered = events_since(events, viewer, since=0)
        assert [cursor for cursor, _ in numbered] == list(range(1, len(numbered) + 1))
        assert view_version(events, viewer) == len(numbered)


def test_events_since_is_exclusive_and_resumes_without_gaps() -> None:
    events = _full_match_events(["alice", "bob"])
    total = view_version(events, ALICE)

    tail = events_since(events, ALICE, since=total - 3)

    assert [cursor for cursor, _ in tail] == [total - 2, total - 1, total]


def test_a_players_cursor_ignores_events_addressed_to_someone_else() -> None:
    state, opening = create_match("m_01", ["alice", "bob"], match_seed=12345)
    before = view_version(opening, ALICE)

    _, produced = transition(
        state,
        "bob",
        SelectCardAction(card=state.players[1].hand[0]),
        MatchProtocol(),
        GameRules(),
    )
    bobs_private = [event for event in produced if event.audience == "player:bob"]

    assert bobs_private
    assert view_version([*opening, *bobs_private], ALICE) == before


def test_a_player_viewer_requires_a_player_id() -> None:
    with pytest.raises(ValueError, match="needs a player_id"):
        Viewer(role=ViewRole.PLAYER)
