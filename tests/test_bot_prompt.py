"""Decision-focused prompts preserve visible information without transport metadata."""

import pytest

from sixnimmt.arena.bots.prompt import SYSTEM_PROMPT, action_tools, observation_text, system_instructions
from sixnimmt.engine.audience import Viewer
from sixnimmt.engine.events import RowChoiceMadeEvent, RowChoiceRequiredEvent
from sixnimmt.engine.fold import ViewFolder, build_view
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.views import MessageView, PlayHistoryView, PrivateMessageView, RevealedCardView, ViewRole


@pytest.mark.parametrize("communication", [False, True])
def test_injects_only_active_mode_and_actual_settings(communication: bool) -> None:
    protocol = MatchProtocol(
        communication_enabled=communication,
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
    assert ("Communication enabled:" in text) == communication
    assert ("Messaging is optional." in text) == communication
    assert ("Classic mode:" in text) != communication
    assert ("maximum 73 characters" in text) == communication
    assert ("table messages only" in text) == communication
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


def test_communication_renders_message_audiences_and_budget() -> None:
    _, events = create_match("prompt", ["a", "b", "c"], 123, protocol=MatchProtocol(communication_enabled=True))
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


def test_observation_distinguishes_attributed_placements_captures_and_pending_cards() -> None:
    _, events = create_match("prompt", ["a", "b", "c"], 123)
    view = build_view(events, Viewer(ViewRole.PLAYER, "b")).model_copy(
        update={
            "revealed_this_hand": ((1, 2, 104),),
            "awaiting_card": 2,
            "legal_actions": ("choose_row",),
            "play_history": (
                PlayHistoryView(
                    hand_number=1,
                    play_number=1,
                    cards=(
                        RevealedCardView(player_id="a", card=1, row_index=0, captured=(11, 22)),
                        RevealedCardView(player_id="b", card=2),
                        RevealedCardView(player_id="c", card=104),
                    ),
                ),
            ),
        }
    )
    text = observation_text(view)
    assert "a played 1: placed on row 0; took 11, 22 (10 penalty points)" in text
    assert "b played 2: pending placement" in text
    assert "c played 104: pending placement" in text
    history_line = next(line for line in text.splitlines() if line.startswith("Revealed/captured"))
    assert history_line.endswith(": 1")


@pytest.mark.parametrize("strict", [False, True])
def test_row_tool_enumerates_displayed_rows(strict: bool) -> None:
    _, events = create_match("prompt", ["a", "b"], 123)
    view = build_view(events, Viewer(ViewRole.PLAYER, "a")).model_copy(update={"legal_actions": ("choose_row",)})
    parameters = action_tools(view, strict)[0]["function"]["parameters"]
    assert parameters["properties"]["row_index"]["enum"] == [row.index for row in view.rows]


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("strict", [False, True])
def test_message_tool_exposes_permissions_recipients_and_length(direct: bool, strict: bool) -> None:
    protocol = MatchProtocol(communication_enabled=True, allow_direct_messages=direct, max_message_length=73)
    _, events = create_match("prompt", ["a", "b", "c"], 123, protocol=protocol)
    view = build_view(events, Viewer(ViewRole.PLAYER, "a"))
    tool = next(tool for tool in action_tools(view, strict) if tool["function"]["name"] == "send_message")
    parameters = tool["function"]["parameters"]
    properties = parameters["properties"]
    assert properties["body"]["maxLength"] == 73
    assert properties["visibility"]["enum"] == (["table", "direct"] if direct else ["table"])
    assert properties["to_player"]["enum"] == ([None, "b", "c"] if direct else [None])
    assert ("to_player" in parameters["required"]) == strict
