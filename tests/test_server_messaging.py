"""HTTP messaging boundaries, durable records, and serialization outcomes."""

import json
from pathlib import Path

import pytest
from conftest import ADMIN_TOKEN, open_match
from fastapi import FastAPI
from starlette.testclient import TestClient

from sixnimmt_server.engine.audience import Viewer, visible_events
from sixnimmt_server.engine.fold import build_view
from sixnimmt_server.engine.views import ViewRole
from sixnimmt_server.persistence.sink import action_log_path, event_log_path, read_action_log, read_event_log
from sixnimmt_server.server.app import create_app


@pytest.mark.parametrize("endpoint", ["actions", "message"])
@pytest.mark.parametrize("field", ["body", "to_player"])
def test_unrepresentable_messages_cannot_poison_live_or_persisted_reads(
    tmp_path: Path, endpoint: str, field: str
) -> None:
    app = create_app(admin_token=ADMIN_TOKEN, log_directory=tmp_path)
    with TestClient(app) as client:
        match = open_match(client, protocol={"negotiation_enabled": True})
        match.start()
        record = app.state.store.record_for(match.match_id)
        before = record.state.model_dump_json()
        cursor = len(record.stream.events)
        actions_before = read_action_log(action_log_path(tmp_path, match.match_id))
        payload = {"type": "send_message", "visibility": "direct", "to_player": "bob", "body": "hello", field: "\ud800"}
        response = client.post(
            f"/matches/{match.match_id}/{endpoint}",
            headers={**match.headers("alice"), "Content-Type": "application/json"},
            content=json.dumps(payload),
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "MALFORMED_REQUEST"
        assert record.state.model_dump_json() == before
        appended = record.stream.events[cursor:]
        assert appended == []
        assert read_action_log(action_log_path(tmp_path, match.match_id)) == actions_before
        for who in ("alice", "bob", "cara", "omniscient"):
            match.state(who)
            match.events(who)
        persisted = read_event_log(event_log_path(tmp_path, match.match_id))
        for viewer in (Viewer(role=ViewRole.PLAYER, player_id="alice"), Viewer(role=ViewRole.OMNISCIENT_OBSERVER)):
            build_view(persisted, viewer).model_dump_json().encode("utf-8")
            for event in visible_events(persisted, viewer):
                json.dumps(event.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("existence", ["hidden", "visible"])
def test_persisted_direct_messages_obey_role_filtered_reads(tmp_path: Path, existence: str) -> None:
    app = create_app(admin_token=ADMIN_TOKEN, log_directory=tmp_path)
    with TestClient(app) as client:
        match = open_match(
            client,
            protocol={"negotiation_enabled": True, "information_policy": {"private_message_existence": existence}},
        )
        match.start()
        response = match.act("bob", type="send_message", visibility="direct", to_player="cara", body="private words")
        assert response.status_code == 200
        log = read_event_log(event_log_path(tmp_path, match.match_id))
        for who in ("alice", "bob", "cara", "spectator", "omniscient", "admin"):
            role = {
                "spectator": ViewRole.PUBLIC_SPECTATOR,
                "omniscient": ViewRole.OMNISCIENT_OBSERVER,
                "admin": ViewRole.ADMIN,
            }.get(who, ViewRole.PLAYER)
            viewer = Viewer(role=role, player_id=who if role == ViewRole.PLAYER else None)
            content_allowed = who in ("bob", "cara", "omniscient", "admin")
            assert ("private words" in json.dumps(match.events(who))) == content_allowed
            assert (
                "private words" in json.dumps([event.model_dump(mode="json") for event in visible_events(log, viewer)])
            ) == content_allowed
            view = build_view(log, viewer)
            assert len(view.messages) == int(content_allowed)
            assert len(view.private_messages_observed) == int(not content_allowed and existence == "visible")


@pytest.mark.parametrize("budget", [None, 3])
def test_hidden_dm_neither_changes_nonparty_responses_nor_notifies_subscriptions(
    client: TestClient, app: FastAPI, budget: int | None
) -> None:
    match = open_match(
        client,
        protocol={
            "negotiation_enabled": True,
            "max_actions_per_play": budget,
            "information_policy": {"private_message_existence": "hidden"},
        },
    )
    match.start()
    assert match.act("alice", type="select_card", card=match.state("alice")["you"]["hand"][0]).status_code == 200
    assert match.act("alice", type="commit").status_code == 200
    record = app.state.store.record_for(match.match_id)
    viewer = Viewer(role=ViewRole.PLAYER, player_id="alice")
    subscription = record.stream.subscribe(viewer)
    urls = [f"/matches/{match.match_id}/state", f"/matches/{match.match_id}/events"]
    before = [client.get(url, headers=match.headers("alice")).content for url in urls]
    try:
        for index in range(3):
            response = match.act("bob", type="send_message", visibility="direct", to_player="cara", body=str(index))
            assert response.status_code == 200
            assert [client.get(url, headers=match.headers("alice")).content for url in urls] == before
            assert not subscription.signal.is_set()
            assert not subscription.pending
            assert subscription.cursor == match.state("alice")["view_version"]
    finally:
        record.stream.unsubscribe(subscription)


@pytest.mark.parametrize("endpoint", ["actions", "message"])
@pytest.mark.parametrize(
    "change", [{"body": "different"}, {"to_player": "cara"}, {"visibility": "table", "to_player": None}]
)
def test_message_idempotency_returns_cached_view_and_rejects_changed_payload(
    client: TestClient, endpoint: str, change: dict
) -> None:
    match = open_match(client, protocol={"negotiation_enabled": True})
    match.start()
    payload = {
        "type": "send_message",
        "action_id": "message-1",
        "visibility": "direct",
        "to_player": "bob",
        "body": "hello",
    }
    url = f"/matches/{match.match_id}/{endpoint}"
    first = client.post(url, headers=match.headers("alice"), json=payload)
    assert first.status_code == 200
    assert match.act("bob", type="send_message", visibility="table", body="later").status_code == 200
    retry = client.post(url, headers=match.headers("alice"), json={**payload, "expected_view_version": -1})
    assert retry.content == first.content
    assert match.state("alice")["you"]["actions_taken_this_play"] == 1
    changed = client.post(url, headers=match.headers("alice"), json={**payload, **change})
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_rejected_message_retries_return_the_cached_rejection(client: TestClient) -> None:
    match = open_match(client, protocol={"negotiation_enabled": True, "max_message_length": 1})
    match.start()
    payload = {"type": "send_message", "visibility": "table", "body": "too long", "action_id": "rejected"}
    first = match.act("alice", **payload)
    assert first.json()["error"]["code"] == "MESSAGE_TOO_LONG"
    assert match.act("bob", type="send_message", visibility="table", body="x").status_code == 200
    before = match.events("alice")
    retry = match.act("alice", **payload, expected_view_version=-1)
    assert retry.content == first.content
    assert match.events("alice") == before


def test_phase_rejection_precedes_message_length_validation(client: TestClient) -> None:
    match = open_match(client, protocol={"negotiation_enabled": True, "max_message_length": 1})
    response = match.act("alice", type="send_message", visibility="table", body="too long")
    assert response.json()["error"]["code"] == "MATCH_NOT_STARTED"
    match.start()
    # With this seed the lowest selected card needs a row choice.
    for who in match.players:
        assert match.act(who, type="select_card", card=match.state(who)["you"]["hand"][0]).status_code == 200
        assert match.act(who, type="commit").status_code == 200
    assert match.state("alice")["phase"] == "awaiting_row_choice"
    for who in match.players:
        response = match.act(who, type="send_message", visibility="table", body="too long")
        assert response.json()["error"]["code"] == "NOT_YOUR_TURN"


def test_five_hundred_messages_have_a_bounded_view_and_complete_log(tmp_path: Path) -> None:
    app = create_app(admin_token=ADMIN_TOKEN, log_directory=tmp_path)
    with TestClient(app) as client:
        match = open_match(client, protocol={"negotiation_enabled": True})
        match.start()
        for index in range(500):
            response = match.act("alice", type="send_message", visibility="table", body=str(index))
            assert response.status_code == 200
        view = response.json()
        assert [message["body"] for message in view["messages"]] == [str(index) for index in range(400, 500)]
        assert view["messages_omitted"] == 400
        assert view["you"]["actions_taken_this_play"] == 500
        log = read_event_log(event_log_path(tmp_path, match.match_id))
        assert sum(event.type == "message_sent" for event in log) == 500
        assert match.act("bob", type="select_card", card=match.state("bob")["you"]["hand"][0]).status_code == 200
        assert match.abandon().status_code == 204


@pytest.mark.parametrize("guarded", [True, False])
def test_message_before_final_commit_conflicts_or_exhausts_its_budget(client: TestClient, guarded: bool) -> None:
    match = open_match(
        client, players=["alice", "bob"], protocol={"negotiation_enabled": True, "max_actions_per_play": 2}
    )
    match.start()
    for who in match.players:
        assert match.act(who, type="select_card", card=match.state(who)["you"]["hand"][0]).status_code == 200
    assert match.act("alice", type="commit").status_code == 200
    version = match.state("bob")["view_version"]
    assert match.act("bob", type="send_message", visibility="table", body="wait").status_code == 200
    response = match.act("bob", type="commit", **({"expected_view_version": version} if guarded else {}))
    assert response.json()["error"]["code"] == ("VERSION_CONFLICT" if guarded else "ACTION_BUDGET_EXHAUSTED")
    assert match.state("alice")["revealed_this_hand"] == []


@pytest.mark.parametrize("guarded", [True, False])
def test_final_commit_before_message_uses_new_play_and_reset_budget(client: TestClient, guarded: bool) -> None:
    match = open_match(
        client, players=["alice", "bob"], protocol={"negotiation_enabled": True, "max_actions_per_play": 2}
    )
    match.start()
    for who in match.players:
        assert match.act(who, type="select_card", card=match.state(who)["you"]["hand"][0]).status_code == 200
    assert match.act("alice", type="commit").status_code == 200
    version = match.state("bob")["view_version"]
    assert match.act("bob", type="commit").status_code == 200
    response = match.act(
        "bob",
        type="send_message",
        visibility="table",
        body="next play",
        **({"expected_view_version": version} if guarded else {}),
    )
    if guarded:
        assert response.json()["error"]["code"] == "VERSION_CONFLICT"
    else:
        assert response.status_code == 200
        assert response.json()["play_number"] == 2
        assert response.json()["you"]["actions_taken_this_play"] == 1
        event = next(event for event in reversed(match.events("bob")) if event["type"] == "message_sent")
        assert (event["hand"], event["play"]) == (1, 2)


def test_schema_exports_internal_event_discriminators_and_message_view(client: TestClient) -> None:
    schemas = client.get("/schemas").json()
    assert "action_counted" in schemas["event"]["discriminator"]["mapping"]
    assert "message_sent" in schemas["event"]["discriminator"]["mapping"]
    assert {"messages", "private_messages_observed", "messages_omitted"} <= schemas["view"]["properties"].keys()


def test_unknown_recipient_diagnostic_is_ascii_escaped_and_safe_to_persist(tmp_path: Path) -> None:
    app = create_app(admin_token=ADMIN_TOKEN, log_directory=tmp_path)
    with TestClient(app) as client:
        match = open_match(client, protocol={"negotiation_enabled": True})
        match.start()
        response = match.act("alice", type="send_message", visibility="direct", to_player="🐂", body="hello")
        assert response.json()["error"]["code"] == "RECIPIENT_NOT_FOUND"
        assert "\\U0001f402" in response.json()["error"]["message"]
        log = read_event_log(event_log_path(tmp_path, match.match_id))
        assert log[-1].type == "action_rejected"
        assert log[-1].data["message"].isascii()
        for who in ("alice", "bob", "omniscient"):
            match.events(who)
            match.state(who)
