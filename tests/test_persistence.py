"""The durable match log: written as it happens, and sufficient to rebuild the match."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import ADMIN_TOKEN, Match, open_match, play_to_completion
from fastapi import FastAPI
from starlette.testclient import TestClient

from sixnimmt_server.engine.replay import replay_events
from sixnimmt_server.server.app import create_app
from sixnimmt_server.server.sink import (
    action_log_path,
    event_log_path,
    read_action_log,
    read_event_log,
)
from sixnimmt_server.server.store import MatchRecord


@pytest.fixture
def logged(tmp_path: Path) -> Iterator[Match]:
    """A started match on a server that writes its logs to a temporary directory."""
    app = create_app(admin_token=ADMIN_TOKEN, log_directory=tmp_path)
    with TestClient(app) as client:
        match = open_match(client)
        match.start()
        yield match


def _record(match: Match) -> MatchRecord:
    """The server's own record, to compare the log against what really happened."""
    app = cast("FastAPI", match.client.app)
    record = app.state.store.record_for(match.match_id)
    assert record is not None
    return record


def _events_file(match: Match, directory: Path) -> Path:
    return event_log_path(directory, match.match_id)


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _card_not_held(match: Match, player_id: str) -> int:
    """A card the engine will refuse, rather than one the action model rejects."""
    hand = match.state(player_id)["you"]["hand"]
    return next(card for card in range(1, 105) if card not in hand)


def test_the_log_exists_and_is_readable_before_the_match_ends(logged: Match, tmp_path: Path) -> None:
    """Flushed as written: a crash mid-match must not cost everything so far."""
    written = _lines(_events_file(logged, tmp_path))

    assert [entry["type"] for entry in written[:2]] == ["match_created", "match_created"]
    assert written[-1]["type"] == "play_started"


def test_each_action_extends_the_log_as_it_is_applied(logged: Match, tmp_path: Path) -> None:
    before = len(_lines(_events_file(logged, tmp_path)))
    hand = logged.state("alice")["you"]["hand"]

    response = logged.act("alice", type="select_card", card=hand[0])

    assert response.status_code == 200
    written = _lines(_events_file(logged, tmp_path))
    assert len(written) > before
    assert [entry["type"] for entry in written[before:]] == [
        "selection_made",
        "selection_registered",
        "player_committed",
    ]


def test_the_log_holds_every_event_including_the_admin_only_ones(logged: Match, tmp_path: Path) -> None:
    """The durable record is unfiltered; audience filtering belongs to reading."""
    written = _lines(_events_file(logged, tmp_path))

    seed_events = [entry for entry in written if entry["type"] == "match_seed_assigned"]
    assert [entry["data"]["match_seed"] for entry in seed_events] == [logged.seed]
    assert all(entry["audience"] == "admin" for entry in seed_events)


def test_replaying_the_written_log_reproduces_the_live_final_state(logged: Match, tmp_path: Path) -> None:
    play_to_completion(logged)
    live = _record(logged).state

    replayed = replay_events(read_event_log(_events_file(logged, tmp_path)))

    live_state = live.model_dump()
    live_state["undealt_remainder"] = sorted(live_state["undealt_remainder"])
    replayed_state = replayed.state.model_dump()
    replayed_state["undealt_remainder"] = sorted(replayed_state["undealt_remainder"])
    assert replayed_state == live_state
    assert replayed.status == "finished"


def test_a_rejected_action_reaches_the_log_without_changing_the_replayed_state(logged: Match, tmp_path: Path) -> None:
    before = replay_events(read_event_log(_events_file(logged, tmp_path))).state

    response = logged.act("alice", type="select_card", card=_card_not_held(logged, "alice"))

    assert response.json()["error"]["code"] == "CARD_NOT_IN_HAND"
    written = _lines(_events_file(logged, tmp_path))
    assert written[-1]["type"] == "action_rejected"
    assert replay_events(read_event_log(_events_file(logged, tmp_path))).state == before


def test_every_submitted_action_is_recorded_in_processing_order(logged: Match, tmp_path: Path) -> None:
    hand = logged.state("alice")["you"]["hand"]
    logged.act("alice", type="select_card", card=hand[0], action_id="a1", from_view="v_seen")
    logged.act("bob", type="select_card", card=_card_not_held(logged, "bob"), action_id="b1")

    records = read_action_log(action_log_path(tmp_path, logged.match_id))

    assert [record.server_action_seq for record in records] == [2, 3]
    assert [(record.player_id, record.type) for record in records] == [
        ("alice", "select_card"),
        # A refused action is recorded too: rejections are counted per player.
        ("bob", "select_card"),
    ]
    assert records[0].from_view == "v_seen"
    assert records[0].action_id == "a1"


def test_the_action_record_names_the_authenticated_player_not_the_body(logged: Match, tmp_path: Path) -> None:
    hand = logged.state("alice")["you"]["hand"]

    logged.act("alice", type="select_card", card=hand[0], player_id="bob")

    records = read_action_log(action_log_path(tmp_path, logged.match_id))
    assert [record.player_id for record in records] == ["alice"]


def test_a_replayed_action_is_recorded_once(logged: Match, tmp_path: Path) -> None:
    hand = logged.state("alice")["you"]["hand"]
    payload = {"type": "select_card", "card": hand[0], "action_id": "same"}

    logged.act("alice", **payload)
    logged.act("alice", **payload)

    records = read_action_log(action_log_path(tmp_path, logged.match_id))
    assert [record.action_id for record in records] == ["same"]


def test_abandoning_a_match_seals_the_log_and_leaves_it_on_disk(logged: Match, tmp_path: Path) -> None:
    hand = logged.state("alice")["you"]["hand"]
    logged.act("alice", type="select_card", card=hand[0])

    assert logged.abandon().status_code == 204

    path = _events_file(logged, tmp_path)
    assert path.exists()
    written = _lines(path)
    assert written[-1]["type"] == "match_abandoned"
    replayed = replay_events(read_event_log(path))
    assert replayed.status == "abandoned"


def test_actions_after_abandonment_add_nothing_to_the_sealed_log(logged: Match, tmp_path: Path) -> None:
    hand = logged.state("alice")["you"]["hand"]
    logged.abandon()
    before = _lines(_events_file(logged, tmp_path))

    response = logged.act("alice", type="select_card", card=hand[0])

    assert response.status_code == 409
    assert _lines(_events_file(logged, tmp_path)) == before


def test_a_server_without_a_log_directory_writes_nothing(tmp_path: Path) -> None:
    app = create_app(admin_token=ADMIN_TOKEN)
    with TestClient(app) as client:
        match = open_match(client)
        match.start()
        play_to_completion(match)

    assert list(tmp_path.iterdir()) == []


def _comparable_log(path: Path) -> list[dict[str, Any]]:
    """A log stripped of everything two runs of the same match cannot share.

    Timestamps and the generated match id differ between runs by construction.
    Replay never reads either of them: §11.1 makes `server_action_seq` the
    canonical order.
    """
    entries = _lines(path)
    for entry in entries:
        entry.pop("timestamp")
        entry.pop("match_id")
    return entries


def test_the_same_seed_and_actions_produce_identical_logs(tmp_path: Path) -> None:
    paths = []
    for run in ("first", "second"):
        directory = tmp_path / run
        app = create_app(admin_token=ADMIN_TOKEN, log_directory=directory)
        with TestClient(app) as client:
            match = open_match(client, seed=31337)
            match.start()
            play_to_completion(match)
            paths.append(_events_file(match, directory))

    assert _comparable_log(paths[0]) == _comparable_log(paths[1])
