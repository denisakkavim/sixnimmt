"""Reading state and events: what a view carries, and what it must never carry."""

from typing import Any

import pytest
from conftest import Match, advance_one_decision, open_match, play_to_completion
from starlette.testclient import TestClient

# Field names that would mean a global counter had reached a player (§10.2).
GLOBAL_COUNTER_FIELDS = {"seq", "server_action_seq", "global_version", "mutation_version", "version"}


def cards_in(view: dict[str, Any]) -> set[int]:
    """Only the fields that actually carry card numbers.

    Scraping every integer would flag scores, counters and cursors as if they
    were cards, and the false positives would bury any real leak.
    """
    cards = {*view["you"]["hand"], *view["you"]["penalty_cards"]}
    if view["you"]["selection"] is not None:
        cards.add(view["you"]["selection"])
    cards.update(card for row in view["rows"] for card in row["cards"])
    cards.update(card for opponent in view["players"] for card in opponent["penalty_cards"])
    cards.update(card for revealed in view["revealed_this_hand"] for card in revealed)
    return cards


def field_names(payload: Any) -> set[str]:
    """Every field name at any depth of a response."""
    if isinstance(payload, dict):
        return set(payload) | {name for value in payload.values() for name in field_names(value)}
    if isinstance(payload, list):
        return {name for item in payload for name in field_names(item)}
    return set()


def test_state_reports_the_callers_own_hand_and_the_shared_table(match: Match) -> None:
    view = match.state("alice")

    assert view["match_id"] == match.match_id
    assert view["status"] == "in_progress"
    assert view["phase"] == "selecting"
    assert len(view["you"]["hand"]) == 10
    assert [row["index"] for row in view["rows"]] == [0, 1, 2, 3]
    assert {opponent["player_id"] for opponent in view["players"]} == {"bob", "cara"}
    assert view["legal_actions"] == ["select_card"]


def test_no_player_view_ever_carries_another_players_hand_card(match: Match) -> None:
    while True:
        hands = {player_id: set(match.state(player_id)["you"]["hand"]) for player_id in match.players}
        for player_id in match.players:
            others = {card for other, hand in hands.items() if other != player_id for card in hand}

            assert cards_in(match.state(player_id)).isdisjoint(others)

        if not advance_one_decision(match):
            return


def test_no_opponent_card_is_visible_before_the_reveal(match: Match) -> None:
    alice_card = match.state("alice")["you"]["hand"][0]
    match.act("alice", type="select_card", card=alice_card)

    for observer in ("bob", "cara", "spectator"):
        view = match.state(observer)

        assert alice_card not in cards_in(view)
        alice_seat = next(seat for seat in view["players"] if seat["player_id"] == "alice")
        assert alice_seat["has_selection"] is True
        assert alice_seat["committed"] is True
        assert alice_seat["selection"] is None


def test_the_undealt_remainder_reaches_no_view_at_all(client: TestClient) -> None:
    two_handed = open_match(client, ["alice", "bob"])
    two_handed.start()
    dealt = {card for player_id in two_handed.players for card in two_handed.state(player_id)["you"]["hand"]}
    dealt.update(card for row in two_handed.state("alice")["rows"] for card in row["cards"])
    remainder = set(range(1, 105)) - dealt

    for who in [*two_handed.players, "spectator", "omniscient"]:
        assert cards_in(two_handed.state(who)).isdisjoint(remainder)


@pytest.mark.parametrize("who", ["alice", "spectator"], ids=["player", "public_spectator"])
def test_no_global_counter_appears_in_a_response_at_any_depth(match: Match, who: str) -> None:
    play_to_completion(match)

    state_fields = field_names(match.state(who))
    event_fields = field_names(match.events(who))

    assert state_fields.isdisjoint(GLOBAL_COUNTER_FIELDS)
    assert event_fields.isdisjoint(GLOBAL_COUNTER_FIELDS)


def test_omniscient_observers_and_admin_keep_the_global_sequence(match: Match) -> None:
    """§10.2 grants `seq` to these two roles, and to nobody else."""
    for who in ("omniscient", "admin"):
        assert all("seq" in event for event in match.events(who))


def test_each_players_stream_is_contiguous_from_one_across_a_whole_match(match: Match) -> None:
    play_to_completion(match)

    for who in [*match.players, "spectator"]:
        cursors = [event["view_version"] for event in match.events(who)]

        assert cursors == list(range(1, len(cursors) + 1))


def test_events_since_is_exclusive_and_resumes_without_a_gap(match: Match) -> None:
    everything = match.events("alice")

    resumed = match.events("alice", since=3)

    assert [event["view_version"] for event in resumed] == [event["view_version"] for event in everything[3:]]


def test_event_limit_truncates_without_renumbering(match: Match) -> None:
    limited = match.client.get(
        f"/matches/{match.match_id}/events?since=0&limit=2", headers=match.headers("alice")
    ).json()

    assert [event["view_version"] for event in limited["events"]] == [1, 2]
    assert limited["view_version"] == len(match.events("alice"))


def test_admin_events_carry_the_seed_but_no_player_stream_does(match: Match) -> None:
    admin_events = match.events("admin")

    seeds = [event for event in admin_events if event["type"] in {"match_seed_assigned", "hand_seed_assigned"}]
    assert seeds
    for who in [*match.players, "spectator", "omniscient"]:
        types = {event["type"] for event in match.events(who)}
        assert "match_seed_assigned" not in types
        assert "hand_seed_assigned" not in types


def test_agent_metadata_reaches_the_log_but_not_another_player(client: TestClient) -> None:
    created = open_match(
        client,
        [
            {"id": "alice", "agent_metadata": {"model": "claude-opus-5"}},
            {"id": "bob", "agent_metadata": {"model": "secret-model"}},
        ],
    )
    created.start()

    assert (
        "secret-model"
        in created.client.get(f"/matches/{created.match_id}/events", headers=created.headers("admin")).text
    )
    for who in ("alice", "spectator", "omniscient"):
        assert (
            "secret-model"
            not in created.client.get(f"/matches/{created.match_id}/events", headers=created.headers(who)).text
        )
