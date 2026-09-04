"""Dealing order and the undealt remainder."""

import pytest

from sixnimmt_server.engine.cards import deal, full_deck, shuffled_deck


def test_deal_is_round_robin_in_seating_order_for_10_passes() -> None:
    deck = full_deck()
    player_ids = ["p1", "p2", "p3"]

    hands, _, _ = deal(deck, player_ids)

    assert hands["p1"] == [deck[i] for i in range(0, 30, 3)]
    assert hands["p2"] == [deck[i] for i in range(1, 30, 3)]
    assert hands["p3"] == [deck[i] for i in range(2, 30, 3)]


def test_next_4_cards_become_row_starts_in_order() -> None:
    deck = full_deck()

    _, row_starts, _ = deal(deck, ["p1", "p2"])

    assert row_starts == [deck[20], deck[21], deck[22], deck[23]]


@pytest.mark.parametrize(
    ("player_count", "expected_remainder"),
    [(2, 80), (5, 50), (10, 0)],
)
def test_remainder_size_depends_on_player_count(player_count: int, expected_remainder: int) -> None:
    player_ids = [f"p{i}" for i in range(player_count)]

    _, _, remainder = deal(full_deck(), player_ids)

    assert len(remainder) == expected_remainder


def test_2_player_deal_leaves_80_unused_cards() -> None:
    hands, row_starts, remainder = deal(full_deck(), ["alice", "bob"])

    assert all(len(hand) == 10 for hand in hands.values())
    assert len(row_starts) == 4
    assert len(remainder) == 80


def test_10_players_consume_the_deck_exactly() -> None:
    player_ids = [f"p{i}" for i in range(10)]

    hands, row_starts, remainder = deal(full_deck(), player_ids)

    dealt = [card for hand in hands.values() for card in hand]
    assert sorted(dealt + row_starts) == full_deck()
    assert remainder == []


def test_every_card_lands_in_exactly_one_place() -> None:
    hands, row_starts, remainder = deal(full_deck(), ["a", "b", "c", "d", "e"])

    all_cards = [card for hand in hands.values() for card in hand]
    all_cards.extend(row_starts)
    all_cards.extend(remainder)

    assert sorted(all_cards) == full_deck()


def test_hands_keep_deal_order_not_sorted() -> None:
    deck = shuffled_deck(12345, 1)

    hands, _, _ = deal(deck, ["a", "b"])

    assert hands["a"] == [deck[i] for i in range(0, 20, 2)]
    assert hands["a"] != sorted(hands["a"])
