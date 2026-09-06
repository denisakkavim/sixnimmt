"""Communication communication, accounting, and audience-derived views."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from sixnimmt.engine.actions import (
    ChooseRowAction,
    CommitAction,
    SelectCardAction,
    SendMessageAction,
    UncommitAction,
)
from sixnimmt.engine.audience import Viewer, visible_events
from sixnimmt.engine.errors import EngineRejection, ErrorCode
from sixnimmt.engine.events import PlayStartedEvent
from sixnimmt.engine.fold import MAX_VIEW_MESSAGES, build_view
from sixnimmt.engine.replay import replay_events
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.state import Phase, ResolutionState
from sixnimmt.engine.transition import transition
from sixnimmt.engine.views import ViewRole
from sixnimmt.persistence.sink import read_event_log

RULES = GameRules()
COMMUNICATION = MatchProtocol(communication_enabled=True)


@pytest.mark.parametrize("body", ["", "hello", "🐂é\n你好"])
def test_table_message_changes_only_the_senders_action_count(body: str) -> None:
    state, _ = create_match("m", ["alice", "bob"], 12345, protocol=COMMUNICATION)
    updated, events = transition(state, "alice", SendMessageAction(visibility="table", body=body), COMMUNICATION, RULES)
    expected = state.model_copy(
        update={
            "players": (
                state.players[0].model_copy(update={"actions_taken_this_play": 1}),
                state.players[1],
            )
        }
    )
    assert updated == expected
    assert [event.type for event in events] == ["action_counted", "message_sent"]
    assert events[0].audience == "player:alice"
    assert events[0].data == {"player_id": "alice", "actions_taken_this_play": 1, "actions_remaining_this_play": None}
    assert events[1].audience == "public"
    assert events[1].data == {"from": "alice", "visibility": "table", "body": body}


@pytest.mark.parametrize("existence", ["visible", "hidden"])
def test_direct_message_emits_separate_content_copies_and_optional_occurrence(existence: str) -> None:
    protocol = MatchProtocol(communication_enabled=True, information_policy={"private_message_existence": existence})
    state, _ = create_match("m", ["alice", "bob", "cara"], 12345, protocol=protocol)
    _, events = transition(
        state, "bob", SendMessageAction(visibility="direct", to_player="cara", body="secret"), protocol, RULES
    )
    contents = [event for event in events if event.type == "message_sent"]
    assert [event.audience for event in contents] == ["player:bob", "player:cara"]
    assert all(
        event.data == {"from": "bob", "to": "cara", "visibility": "direct", "body": "secret"} for event in contents
    )
    occurrences = [event for event in events if event.type == "private_message_occurred"]
    assert len(occurrences) == (1 if existence == "visible" else 0)
    if occurrences:
        assert occurrences[0].audience == "public"
        assert occurrences[0].data == {"from": "bob", "to": "cara"}


@pytest.mark.parametrize(
    ("settings", "phase", "message", "code"),
    [
        ({"communication_enabled": False}, Phase.SELECTING, {}, ErrorCode.COMMUNICATION_DISABLED),
        ({}, Phase.RESOLVING, {}, ErrorCode.WRONG_PHASE),
        ({}, Phase.SELECTING, {"visibility": "direct", "to_player": "alice"}, ErrorCode.MALFORMED_REQUEST),
        ({}, Phase.SELECTING, {"visibility": "direct", "to_player": "absent"}, ErrorCode.RECIPIENT_NOT_FOUND),
        (
            {"allow_direct_messages": False},
            Phase.SELECTING,
            {"visibility": "direct", "to_player": "bob"},
            ErrorCode.DIRECT_MESSAGES_DISABLED,
        ),
        ({"max_message_length": 1}, Phase.SELECTING, {"body": "long"}, ErrorCode.MESSAGE_TOO_LONG),
        ({"max_actions_per_play": 1}, Phase.SELECTING, {}, ErrorCode.ACTION_BUDGET_EXHAUSTED),
    ],
)
def test_message_rejection_preserves_state(settings: dict, phase: Phase, message: dict, code: ErrorCode) -> None:
    protocol = MatchProtocol.model_validate({"communication_enabled": True, **settings})
    state, _ = create_match("m", ["alice", "bob"], 12345, protocol=protocol)
    state = state.model_copy(
        update={
            "phase": phase,
            "players": (
                state.players[0].model_copy(update={"actions_taken_this_play": 1}),
                state.players[1],
            ),
        }
    )
    before = state.model_dump_json()
    action = SendMessageAction.model_validate({"visibility": "table", "body": "", **message})
    with pytest.raises(EngineRejection) as rejected:
        transition(state, "alice", action, protocol, RULES)
    assert rejected.value.code == code
    assert state.model_dump_json() == before


@pytest.mark.parametrize("actor", ["alice", "bob"])
def test_every_player_must_wait_for_row_choice_before_messaging(actor: str) -> None:
    state, _ = create_match("m", ["alice", "bob"], 12345)
    state = state.model_copy(
        update={
            "phase": Phase.AWAITING_ROW_CHOICE,
            "resolution": ResolutionState(
                ordered_cards=((1, "alice"),),
                awaiting_player="alice",
            ),
        }
    )
    with pytest.raises(EngineRejection) as rejected:
        transition(state, actor, SendMessageAction(visibility="table", body=""), COMMUNICATION, RULES)
    assert rejected.value.code == ErrorCode.NOT_YOUR_TURN


@pytest.mark.parametrize("field", ["body", "to_player"])
def test_message_model_refuses_lone_surrogates(field: str) -> None:
    with pytest.raises(ValidationError):
        SendMessageAction.model_validate({"visibility": "direct", "to_player": "bob", "body": "hello", field: "\ud800"})


@pytest.mark.parametrize(
    "role,player_id,content,occurrence",
    [
        (ViewRole.PLAYER, "alice", 0, 1),
        (ViewRole.PLAYER, "bob", 1, 0),
        (ViewRole.PLAYER, "cara", 1, 0),
        (ViewRole.PUBLIC_SPECTATOR, None, 0, 1),
        (ViewRole.OMNISCIENT_OBSERVER, None, 1, 0),
        (ViewRole.ADMIN, None, 1, 0),
    ],
)
def test_each_role_counts_a_direct_message_exactly_once(
    role: ViewRole, player_id: str | None, content: int, occurrence: int
) -> None:
    state, log = create_match("m", ["alice", "bob", "cara"], 12345, protocol=COMMUNICATION)
    for _ in range(MAX_VIEW_MESSAGES + 1):
        state, events = transition(
            state, "bob", SendMessageAction(visibility="direct", to_player="cara", body="secret"), COMMUNICATION, RULES
        )
        log.extend(events)
    view = build_view(log, Viewer(role=role, player_id=player_id))
    assert len(view.messages) == content * MAX_VIEW_MESSAGES
    assert len(view.private_messages_observed) == occurrence * MAX_VIEW_MESSAGES
    assert view.messages_omitted == 1
    if occurrence:
        assert "secret" not in view.model_dump_json()
        assert all("body" not in entry.model_dump() for entry in view.private_messages_observed)


@pytest.mark.parametrize("budget", [None, 1, 2])
def test_hidden_messages_including_budget_exhaustion_leave_nonparty_view_identical(budget: int | None) -> None:
    protocol = MatchProtocol(
        communication_enabled=True,
        max_actions_per_play=budget,
        information_policy={"private_message_existence": "hidden"},
    )
    state, log = create_match("m", ["alice", "bob", "cara"], 12345, protocol=protocol)
    viewer = Viewer(role=ViewRole.PLAYER, player_id="alice")
    before = build_view(log, viewer).model_dump_json()
    visible_before = visible_events(log, viewer)
    for _ in range(budget or 3):
        state, events = transition(
            state, "bob", SendMessageAction(visibility="direct", to_player="cara", body="secret"), protocol, RULES
        )
        log.extend(events)
        assert build_view(log, viewer).model_dump_json() == before
        assert visible_events(log, viewer) == visible_before
    bob = build_view(log, Viewer(role=ViewRole.PLAYER, player_id="bob"))
    assert bob.you.actions_remaining_this_play == (None if budget is None else 0)
    assert bob.legal_actions == (("select_card", "send_message") if budget is None else ())


def test_cap_combines_content_and_occurrences_and_resets_next_play() -> None:
    state, log = create_match("m", ["alice", "bob", "cara"], 12345, protocol=COMMUNICATION)
    for index in range(105):
        message = (
            SendMessageAction(visibility="table", body=str(index))
            if index % 2 == 0
            else SendMessageAction(visibility="direct", to_player="cara", body=str(index))
        )
        state, events = transition(state, "bob", message, COMMUNICATION, RULES)
        log.extend(events)
    for viewer in (Viewer(role=ViewRole.PLAYER, player_id="alice"), Viewer(role=ViewRole.PUBLIC_SPECTATOR)):
        view = build_view(log, viewer)
        assert [message.body for message in view.messages] == [str(index) for index in range(6, 105, 2)]
        assert len(view.private_messages_observed) == 50
        assert view.messages_omitted == 5
        reset = build_view([*log, PlayStartedEvent(match_id="m", audience="public", data={"play": 2})], viewer)
        assert reset.messages == reset.private_messages_observed == ()
        assert reset.messages_omitted == 0


def test_replay_and_own_accounting_match_after_every_communication_action() -> None:
    protocol = MatchProtocol(communication_enabled=True, end_condition="fixed_hands", hands=1, max_actions_per_play=100)
    state, log = create_match("m", ["alice", "bob"], 12345, protocol=protocol)
    # Include actions a basic select/commit bot never exercises.
    initial = [
        SendMessageAction(visibility="direct", to_player="bob", body="secret"),
        SelectCardAction(card=state.players[0].hand[0]),
        CommitAction(),
        UncommitAction(),
    ]
    while state.phase != Phase.FINISHED:
        if initial:
            actor, action = "alice", initial.pop(0)
        elif state.phase == Phase.AWAITING_ROW_CHOICE:
            assert state.resolution is not None and state.resolution.awaiting_player is not None
            actor, action = state.resolution.awaiting_player, ChooseRowAction(row_index=0)
        else:
            player = next(player for player in state.players if not player.committed)
            actor = player.player_id
            action = SelectCardAction(card=player.hand[0]) if player.selection is None else CommitAction()
        state, events = transition(state, actor, action, protocol, RULES)
        log.extend(events)
        replayed = replay_events(log).state
        assert replayed.model_dump(exclude={"undealt_remainder"}) == state.model_dump(exclude={"undealt_remainder"})
        assert sorted(replayed.undealt_remainder) == sorted(state.undealt_remainder)
        for player in state.players:
            view = build_view(log, Viewer(role=ViewRole.PLAYER, player_id=player.player_id))
            assert view.you.actions_taken_this_play == player.actions_taken_this_play
            assert view.you.actions_remaining_this_play == 100 - player.actions_taken_this_play
            if any(event.type == "play_started" for event in events):
                assert view.messages == ()


@pytest.mark.parametrize("communication", [False, True])
def test_legacy_logs_preserve_selection_only_counts(communication: bool) -> None:
    name = "legacy_communication.jsonl" if communication else "legacy_classic.jsonl"
    log = read_event_log(Path(__file__).parent / "fixtures" / name)
    assert all(event.type != "action_counted" for event in log)
    replay = replay_events(log).state
    view = build_view(log, Viewer(role=ViewRole.PLAYER, player_id="alice"))
    # Communication logs historically omitted explicit commits and uncommits from
    # accounting. Compatibility preserves that undercount, not the live total 3.
    assert replay.players[0].actions_taken_this_play == view.you.actions_taken_this_play == 1
    assert view.you.actions_remaining_this_play == 9
    assert replay.players[0].selection == view.you.selection == (None if communication else 96)


def test_budget_exhaustion_does_not_hide_a_required_row_choice() -> None:
    protocol = MatchProtocol(communication_enabled=True, max_actions_per_play=2)
    state, log = create_match("m", ["alice", "bob", "cara"], 12345, protocol=protocol)
    for player in state.players:
        for action in (SelectCardAction(card=player.hand[0]), CommitAction()):
            state, events = transition(state, player.player_id, action, protocol, RULES)
            log.extend(events)
    assert state.phase == Phase.AWAITING_ROW_CHOICE
    assert state.resolution is not None
    for player in state.players:
        view = build_view(log, Viewer(role=ViewRole.PLAYER, player_id=player.player_id))
        assert view.you.actions_remaining_this_play == 0
        assert view.legal_actions == (("choose_row",) if player.player_id == state.resolution.awaiting_player else ())


def test_hidden_messages_do_not_displace_interleaved_visible_entries_at_cap() -> None:
    visible_protocol = COMMUNICATION
    hidden_protocol = MatchProtocol(
        communication_enabled=True, information_policy={"private_message_existence": "hidden"}
    )
    state, log = create_match("m", ["alice", "bob", "cara"], 12345, protocol=visible_protocol)
    baseline = list(log)
    for index in range(103):
        action = (
            SendMessageAction(visibility="table", body=str(index))
            if index % 2 == 0
            else SendMessageAction(visibility="direct", to_player="cara", body=str(index))
        )
        state, events = transition(state, "bob", action, visible_protocol, RULES)
        log.extend(events)
        baseline.extend(events)
        # Vary only invisible events; both viewer histories must fold identically.
        state, hidden = transition(
            state,
            "bob",
            SendMessageAction(visibility="direct", to_player="cara", body="hidden"),
            hidden_protocol,
            RULES,
        )
        log.extend(hidden)
        for viewer in (Viewer(role=ViewRole.PLAYER, player_id="alice"), Viewer(role=ViewRole.PUBLIC_SPECTATOR)):
            assert build_view(log, viewer).model_dump_json() == build_view(baseline, viewer).model_dump_json()


@pytest.mark.parametrize("body,accepted", [("🐂é", True), ("🐂éx", False)])
def test_message_length_counts_unicode_code_points(body: str, accepted: bool) -> None:
    protocol = MatchProtocol(communication_enabled=True, max_message_length=2)
    state, _ = create_match("m", ["alice", "bob"], 12345, protocol=protocol)
    action = SendMessageAction(visibility="table", body=body)
    if accepted:
        _, events = transition(state, "alice", action, protocol, RULES)
        assert events[-1].data["body"] == body
    else:
        with pytest.raises(EngineRejection) as rejected:
            transition(state, "alice", action, protocol, RULES)
        assert rejected.value.code == ErrorCode.MESSAGE_TOO_LONG


@pytest.mark.parametrize(
    "recipient,code",
    [
        ("absent", ErrorCode.RECIPIENT_NOT_FOUND),
        ("alice", ErrorCode.MALFORMED_REQUEST),
        ("bob", ErrorCode.DIRECT_MESSAGES_DISABLED),
    ],
)
def test_recipient_checks_precede_length_and_exhausted_budget(recipient: str, code: ErrorCode) -> None:
    protocol = MatchProtocol(
        communication_enabled=True, allow_direct_messages=False, max_actions_per_play=1, max_message_length=1
    )
    state, _ = create_match("m", ["alice", "bob"], 12345, protocol=protocol)
    state, _ = transition(state, "alice", SendMessageAction(visibility="table", body=""), protocol, RULES)
    with pytest.raises(EngineRejection) as rejected:
        transition(
            state, "alice", SendMessageAction(visibility="direct", to_player=recipient, body="long"), protocol, RULES
        )
    assert rejected.value.code == code
