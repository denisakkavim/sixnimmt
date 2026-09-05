"""Idempotency keyed `(match_id, player_id, action_id)`, and bounded per player."""

from conftest import Match

from sixnimmt_server.server.store import CacheEntry, IdempotencyCache


def test_repeating_an_action_id_applies_once_and_returns_the_original_result(match: Match) -> None:
    card = match.state("alice")["you"]["hand"][0]

    first = match.act("alice", type="select_card", card=card, action_id="once")
    second = match.act("alice", type="select_card", card=card, action_id="once")

    assert first.status_code == 200
    assert second.json() == first.json()
    assert match.state("alice")["you"]["actions_taken_this_play"] == 1


def test_the_same_action_id_from_two_players_are_independent_actions(match: Match) -> None:
    alice_card = match.state("alice")["you"]["hand"][0]
    bob_card = match.state("bob")["you"]["hand"][0]

    alice = match.act("alice", type="select_card", card=alice_card, action_id="shared")
    bob = match.act("bob", type="select_card", card=bob_card, action_id="shared")

    assert alice.status_code == 200
    assert bob.status_code == 200
    assert alice.json()["you"]["selection"] == alice_card
    assert bob.json()["you"]["selection"] == bob_card


def test_reusing_an_action_id_for_a_different_payload_is_refused(match: Match) -> None:
    hand = match.state("alice")["you"]["hand"]
    match.act("alice", type="select_card", card=hand[0], action_id="reused")

    response = match.act("alice", type="select_card", card=hand[1], action_id="reused")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert match.state("alice")["you"]["selection"] == hand[0]


def test_a_retry_that_only_changes_audit_metadata_gets_the_cached_result(match: Match) -> None:
    view = match.state("alice")
    card = view["you"]["hand"][0]

    first = match.act(
        "alice",
        type="select_card",
        card=card,
        action_id="retried",
        from_view=view["view_id"],
        expected_view_version=view["view_version"],
    )
    retried = match.act(
        "alice",
        type="select_card",
        card=card,
        action_id="retried",
        from_view="v_something_else",
        expected_view_version=99,
    )

    assert first.status_code == 200
    assert retried.json() == first.json()


def test_a_refused_action_is_replayed_rather_than_counted_twice(match: Match) -> None:
    first = match.act("alice", type="commit", action_id="bad-once")
    version_after_first = match.state("alice")["view_version"]

    second = match.act("alice", type="commit", action_id="bad-once")

    assert first.json() == second.json()
    assert first.status_code == second.status_code
    assert match.state("alice")["view_version"] == version_after_first


def test_one_players_flood_never_evicts_another_players_entry(match: Match) -> None:
    """A shared bound would make dedup behaviour an oracle for another player's traffic."""
    card = match.state("alice")["you"]["hand"][0]
    original = match.act("alice", type="select_card", card=card, action_id="alice-first")

    for index in range(5000):
        match.act("bob", type="commit", action_id=f"bob-{index}")

    replayed = match.act("alice", type="select_card", card=card, action_id="alice-first")

    assert replayed.json() == original.json()
    assert match.state("alice")["you"]["actions_taken_this_play"] == 1


def test_the_cache_evicts_a_players_oldest_entries_and_nobody_elses() -> None:
    cache = IdempotencyCache(entries_per_player=3)
    alice_entry = CacheEntry(fingerprint="alice", view=None, error=None)
    cache.put("alice", "keep-me", alice_entry)

    for index in range(10):
        cache.put("bob", f"bob-{index}", CacheEntry(fingerprint="bob", view=None, error=None))

    assert cache.get("alice", "keep-me") is alice_entry
    assert cache.get("bob", "bob-0") is None
    assert cache.get("bob", "bob-9") is not None
