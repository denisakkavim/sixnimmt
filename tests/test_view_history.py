"""Shared histories preserve public attribution and each viewer's message permissions."""

import pytest

from sixnimmt_server.arena.bots import GreedyBot
from sixnimmt_server.arena.bots.prompt import observation_text
from sixnimmt_server.arena.runner import run_match
from sixnimmt_server.engine.actions import SelectCardAction, SendMessageAction
from sixnimmt_server.engine.audience import Viewer
from sixnimmt_server.engine.events import CardPlacedEvent, CardsRevealedEvent, PlayStartedEvent, RowTakenEvent
from sixnimmt_server.engine.fold import MAX_HISTORY_PLAYS, MAX_VIEW_MESSAGES, ViewFolder, build_view
from sixnimmt_server.engine.rules import GameRules, MatchProtocol
from sixnimmt_server.engine.setup import create_match
from sixnimmt_server.engine.transition import transition
from sixnimmt_server.engine.views import MessageView, PrivateMessageView, ViewRole


@pytest.mark.parametrize(
    "viewer",
    [
        Viewer(ViewRole.PLAYER, "a"),
        Viewer(ViewRole.PLAYER, "b"),
        Viewer(ViewRole.PUBLIC_SPECTATOR),
    ],
)
def test_revealed_players_and_placement_progress_are_available_to_every_viewer(viewer: Viewer) -> None:
    _, events = create_match("history", ["a", "b"], 123)
    folder = ViewFolder(viewer)
    folder.apply(events)
    folder.apply([CardsRevealedEvent(match_id="history", audience="public", data={"selections": {"a": 80, "b": 1}})])
    before = folder.view()
    cards = before.play_history[-1].cards
    assert [(card.player_id, card.card, card.row_index) for card in cards] == [("b", 1, None), ("a", 80, None)]
    folder.apply([
        RowTakenEvent(match_id="history", audience="public", data={"player_id": "b", "captured": [11, 22]}),
        CardPlacedEvent(match_id="history", audience="public", data={"card": 1, "row": 2, "row_cards": [1]}),
    ])
    cards = folder.view().play_history[-1].cards
    assert cards[0].row_index == 2
    assert cards[0].captured == (11, 22)
    assert cards[1].row_index is None
    # Updating the fold cannot mutate a view already handed to a client.
    assert before.play_history[-1].cards[0].row_index is None
    assert before.play_history[-1].cards[0].captured == ()


def test_selection_is_not_added_to_public_history_before_reveal() -> None:
    state, events = create_match("history", ["a", "b"], 123)
    _, selected = transition(state, "a", SelectCardAction(card=state.players[0].hand[0]), MatchProtocol(), GameRules())
    for viewer in (Viewer(ViewRole.PLAYER, "a"), Viewer(ViewRole.PLAYER, "b"), Viewer(ViewRole.PUBLIC_SPECTATOR)):
        assert build_view([*events, *selected], viewer).play_history == ()


def test_move_history_survives_new_hands_and_is_bounded_and_replayable() -> None:
    result = run_match(
        [GreedyBot(), GreedyBot()],
        123,
        protocol=MatchProtocol(end_condition="fixed_hands", hands=3),
    )
    viewer = Viewer(ViewRole.PLAYER, "player_1")
    folder = ViewFolder(viewer)
    for event in result.events:
        folder.apply([event])
        if event.type == "hand_started" and event.data["hand_number"] == 2:
            view = folder.view()
            assert view.revealed_this_hand == ()
            assert (view.play_history[-1].hand_number, view.play_history[-1].play_number) == (1, 10)
            assert all(card.row_index is not None for card in view.play_history[-1].cards)
    view = folder.view()
    assert len(view.play_history) == MAX_HISTORY_PLAYS
    assert (view.play_history[0].hand_number, view.play_history[0].play_number) == (2, 1)
    assert view == build_view(result.events, viewer)
    reveals = [event for event in result.events if event.type == "cards_revealed"][-MAX_HISTORY_PLAYS:]
    for play, reveal in zip(view.play_history, reveals, strict=True):
        assert {card.player_id: card.card for card in play.cards} == reveal.data["selections"]
        assert [card.card for card in play.cards] == sorted(reveal.data["selections"].values())
        assert all(card.row_index is not None for card in play.cards)
    for other in (Viewer(ViewRole.PLAYER, "player_2"), Viewer(ViewRole.PUBLIC_SPECTATOR)):
        assert build_view(result.events, other).play_history == view.play_history


@pytest.mark.parametrize("existence", ["visible", "hidden"])
@pytest.mark.parametrize(
    "viewer",
    [
        Viewer(ViewRole.PLAYER, "a"),
        Viewer(ViewRole.PLAYER, "b"),
        Viewer(ViewRole.PLAYER, "c"),
        Viewer(ViewRole.PUBLIC_SPECTATOR),
        Viewer(ViewRole.OMNISCIENT_OBSERVER),
        Viewer(ViewRole.ADMIN),
    ],
)
def test_message_history_retains_only_authorized_information_across_plays(existence: str, viewer: Viewer) -> None:
    protocol = MatchProtocol(negotiation_enabled=True, information_policy={"private_message_existence": existence})
    state, events = create_match("history", ["a", "b", "c"], 123, protocol=protocol)
    _, messages = transition(
        state,
        "b",
        SendMessageAction(visibility="direct", to_player="c", body="Promise: play 80."),
        protocol,
        GameRules(),
    )
    folder = ViewFolder(viewer)
    folder.apply(events)
    folder.apply(messages)
    folder.apply([PlayStartedEvent(match_id="history", audience="public", data={"play": 2})])
    view = folder.view()
    assert view.messages == ()
    assert view.private_messages_observed == ()
    can_read = viewer.player_id in ("b", "c") or viewer.role in (ViewRole.ADMIN, ViewRole.OMNISCIENT_OBSERVER)
    if can_read:
        assert len(view.message_history) == 1
        entry = view.message_history[0]
        assert (entry.hand_number, entry.play_number) == (1, 1)
        assert isinstance(entry.message, MessageView)
        assert entry.message.body == "Promise: play 80."
        assert 'Hand 1, play 1: b → c: "Promise: play 80."' in observation_text(view)
    else:
        assert "Promise: play 80." not in view.model_dump_json()
        assert "Promise: play 80." not in observation_text(view)
        if existence == "visible":
            assert len(view.message_history) == 1
            assert isinstance(view.message_history[0].message, PrivateMessageView)
        else:
            assert view.message_history == ()


def test_message_history_is_bounded_across_play_resets() -> None:
    protocol = MatchProtocol(negotiation_enabled=True)
    state, events = create_match("history", ["a", "b"], 123, protocol=protocol)
    folder = ViewFolder(Viewer(ViewRole.PLAYER, "a"))
    folder.apply(events)
    for index in range(MAX_VIEW_MESSAGES + 1):
        state, messages = transition(
            state, "b", SendMessageAction(visibility="table", body=str(index)), protocol, GameRules()
        )
        folder.apply([PlayStartedEvent(match_id="history", audience="public", data={"play": 1 + index % 10})])
        folder.apply(messages)
    history = folder.view().message_history
    assert len(history) == MAX_VIEW_MESSAGES
    assert isinstance(history[0].message, MessageView)
    assert history[0].message.body == "1"
