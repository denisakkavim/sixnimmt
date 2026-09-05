"""§5.3: nothing a player cannot see may reach them through metadata.

Cursors, event counts, response shapes and identifiers must all be unchanged by
activity the caller is not entitled to observe.
"""

from typing import Any

from conftest import Match, advance_one_decision, open_match, play_to_completion
from starlette.testclient import TestClient


def public_types(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events if event["audience"] == "public"]


def without_timestamps(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Events with the one field that legitimately varies between runs removed."""
    return [{name: value for name, value in event.items() if name != "timestamp"} for event in events]


def comparable(payload: Any) -> Any:
    """A response stripped of the identity of the run that produced it.

    Two executions necessarily use different match ids and wall clocks; every
    other byte must match, which is what response-shape equivalence asserts.
    """
    if isinstance(payload, dict):
        return {name: comparable(value) for name, value in payload.items() if name not in {"match_id", "timestamp"}}
    if isinstance(payload, list):
        return [comparable(item) for item in payload]
    return payload


def test_another_players_rejected_actions_do_not_move_this_players_cursor(match: Match) -> None:
    before_version = match.state("alice")["view_version"]
    before_events = match.events("alice")

    for _ in range(20):
        response = match.act("bob", type="select_card", card=104, action_id=None)
        assert response.status_code in (400, 409)

    assert match.state("alice")["view_version"] == before_version
    assert without_timestamps(match.events("alice")) == without_timestamps(before_events)


def test_a_rejected_action_advances_only_the_offending_players_cursor(match: Match) -> None:
    alice_before = match.state("alice")["view_version"]
    bob_before = match.state("bob")["view_version"]

    match.act("bob", type="commit")

    assert match.state("alice")["view_version"] == alice_before
    assert match.state("bob")["view_version"] == bob_before + 1
    assert match.events("bob")[-1]["type"] == "action_rejected"


def test_a_rejection_is_visible_to_nobody_but_the_offender(match: Match) -> None:
    match.act("bob", type="commit")

    for observer in ("alice", "cara", "spectator"):
        assert "action_rejected" not in {event["type"] for event in match.events(observer)}
    assert "action_rejected" in {event["type"] for event in match.events("omniscient")}


def test_each_reselection_shows_the_table_the_same_public_shadow(match: Match) -> None:
    """Bob changing his card five times looks like five identical changes.

    That Bob has a selection is public (§5.1); which card it is never becomes
    observable, and neither does whether a particular change moved it.
    """
    hand = match.state("bob")["you"]["hand"]
    match.act("bob", type="select_card", card=hand[0])
    shadows = []

    for card in hand[1:6]:
        before = match.state("alice")["view_version"]
        match.act("bob", type="select_card", card=card)
        shadows.append(public_types(match.events("alice", since=before)))

    assert shadows[0] == ["selection_cleared", "player_uncommitted", "selection_registered", "player_committed"]
    assert all(shadow == shadows[0] for shadow in shadows)
    # The shadow names who acted and nothing else; no card field ever rides along.
    assert all(
        event["data"] == {"player_id": "bob"}
        for event in match.events("alice", since=1)
        if event["audience"] == "public" and event["type"] in shadows[0]
    )


def test_the_public_shadow_of_a_reselection_does_not_reveal_whether_the_card_moved(client: TestClient) -> None:
    """Re-picking the same card and switching cards must be indistinguishable."""
    streams = []
    for switch in (False, True):
        game = open_match(client)
        game.start()
        hand = game.state("bob")["you"]["hand"]
        game.act("bob", type="select_card", card=hand[0])
        before = game.state("alice")["view_version"]
        game.act("bob", type="select_card", card=hand[1] if switch else hand[0])
        streams.append(comparable(game.events("alice", since=before)))

    assert streams[0] == streams[1]


def test_alices_responses_are_identical_whether_or_not_bob_flails(client: TestClient) -> None:
    """Response-shape equivalence across executions differing only by hidden events."""
    transcripts = []
    for bob_flails in (False, True):
        game = open_match(client)
        game.start()
        observed = [game.state("alice")]
        if bob_flails:
            for _ in range(5):
                game.act("bob", type="commit")
        observed.append(game.state("alice"))
        game.act("alice", type="select_card", card=game.state("alice")["you"]["hand"][0])
        observed.append(game.state("alice"))
        failed = game.act("alice", type="choose_row", row_index=0)
        observed.append(failed.json())
        observed.append(game.state("alice"))
        transcripts.append(comparable(observed))

    assert transcripts[0] == transcripts[1]


def test_error_responses_carry_the_callers_own_cursor(match: Match) -> None:
    match.act("bob", type="commit")
    alice_version = match.state("alice")["view_version"]

    refused = match.act("alice", type="choose_row", row_index=0).json()["error"]

    assert refused["view_version"] == alice_version + 1
    assert refused["view_version"] == match.state("alice")["view_version"]
    assert "seq" not in refused


def test_two_players_watching_the_same_event_receive_different_view_ids(match: Match) -> None:
    alice = match.state("alice")
    bob = match.state("bob")

    assert alice["view_version"] == bob["view_version"]
    assert alice["view_id"] != bob["view_id"]


def test_a_view_id_does_not_move_with_activity_the_caller_cannot_see(match: Match) -> None:
    before = match.state("alice")
    global_before = match.state("admin")["view_version"]

    for _ in range(5):
        match.act("bob", type="commit")

    after = match.state("alice")
    assert match.state("admin")["view_version"] > global_before
    assert after["view_version"] == before["view_version"]
    assert after["view_id"] == before["view_id"]


def test_view_ids_across_a_match_never_track_the_global_sequence(match: Match) -> None:
    seen: dict[str, str] = {}
    play_to_completion(match)

    for event in match.events("admin"):
        alice = match.state("alice")
        seen[str(event["seq"])] = alice["view_id"]

    assert len(set(seen.values())) == 1  # Alice's view stopped moving; the log did not


def test_any_change_in_what_a_player_may_do_arrives_with_an_event_they_can_see(match: Match) -> None:
    """The §5.3 corollary, as a test rather than a claim.

    A gap-free per-viewer cursor only works if nothing that matters to a viewer
    can change without an event reaching them.
    """
    watched = {player_id: match.state(player_id) for player_id in match.players}

    while advance_one_decision(match):
        for player_id in match.players:
            before = watched[player_id]
            after = match.state(player_id)
            if after["legal_actions"] != before["legal_actions"]:
                assert after["view_version"] > before["view_version"], player_id
            watched[player_id] = after
