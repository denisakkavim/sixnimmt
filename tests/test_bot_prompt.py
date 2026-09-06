"""Decision-focused prompts preserve visible information without transport metadata."""

import pytest

from sixnimmt_server.arena.bots.prompt import SYSTEM_PROMPT, observation_text, system_instructions
from sixnimmt_server.engine.audience import Viewer
from sixnimmt_server.engine.events import RowChoiceMadeEvent, RowChoiceRequiredEvent
from sixnimmt_server.engine.fold import ViewFolder, build_view
from sixnimmt_server.engine.rules import MatchProtocol
from sixnimmt_server.engine.setup import create_match
from sixnimmt_server.engine.views import MessageView, PrivateMessageView, ViewRole


@pytest.mark.parametrize("negotiation", [False, True])
def test_injects_only_active_mode_and_actual_settings(negotiation: bool) -> None:
    protocol = MatchProtocol(
        negotiation_enabled=negotiation,
        end_condition="fixed_hands",
        hands=3,
        allow_direct_messages=False,
        max_message_length=73,
        max_actions_per_play=8,
    )
    _, events = create_match("prompt", ["a", "b"], 123, protocol=protocol)
    view = build_view(events, Viewer(ViewRole.PLAYER, "a"))
    text = system_instructions(view, SYSTEM_PROMPT, "Be diplomatic.")
    assert "3 hands" in text
    assert "66 banked" not in text
    assert ("Negotiation mode:" in text) == negotiation
    assert ("Classic mode:" in text) != negotiation
    assert ("maximum 73 characters" in text) == negotiation
    assert ("table messages only" in text) == negotiation
    assert text.endswith("Strategy and personality: Be diplomatic.")


def test_observation_omits_metadata_and_keeps_visible_history() -> None:
    _, events = create_match("private-match-id", ["a", "b"], 123)
    view = build_view(events, Viewer(ViewRole.PLAYER, "a"))
    table_card = view.rows[0].cards[0]
    past_card = next(card for card in range(1, 105) if card not in {c for row in view.rows for c in row.cards})
    view = view.model_copy(update={"revealed_this_hand": ((table_card, past_card),)})
    text = observation_text(view)
    assert "Your cards: " + ", ".join(map(str, sorted(view.you.hand))) in text
    assert "Scores (banked + this hand):" in text
    assert f"outside the current rows: {past_card}" in text
    assert view.match_id not in text
    assert view.view_id not in text
    assert "view_version" not in text
    assert "Your selection" not in text


def test_negotiation_renders_message_audiences_and_budget() -> None:
    _, events = create_match("prompt", ["a", "b", "c"], 123, protocol=MatchProtocol(negotiation_enabled=True))
    view = build_view(events, Viewer(ViewRole.PLAYER, "a"))
    view = view.model_copy(
        update={
            "you": view.you.model_copy(update={"selection": 12, "actions_remaining_this_play": 2}),
            "messages": (MessageView(from_player="b", visibility="direct", to_player="a", body="Deal?\nChoose 12."),),
            "private_messages_observed": (PrivateMessageView(from_player="b", to_player="c"),),
            "messages_omitted": 4,
        }
    )
    text = observation_text(view)
    assert "Your selection: 12" in text
    assert "Actions remaining this play: 2" in text
    assert 'b → a: "Deal?\\nChoose 12."' in text
    assert "b → c: [private message; body hidden]" in text
    assert "Older messages omitted: 4" in text


def test_row_choice_observation_uses_public_triggering_card_and_clears_it() -> None:
    _, events = create_match("prompt", ["a", "b"], 123)
    folder = ViewFolder(Viewer(ViewRole.PLAYER, "a"))
    folder.apply(events)
    folder.apply([RowChoiceRequiredEvent(match_id="prompt", audience="public", data={"player_id": "a", "card": 1})])
    assert "Your played card: 1." in observation_text(folder.view())
    folder.apply([RowChoiceMadeEvent(match_id="prompt", audience="public", data={"player_id": "a", "row": 0})])
    assert folder.view().awaiting_card is None
