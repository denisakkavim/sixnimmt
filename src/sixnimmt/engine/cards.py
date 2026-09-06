"""Deck construction, bull head values, seeded shuffling, and dealing."""

import hashlib
import random


def bull_heads(card: int) -> int:
    if not 1 <= card <= 104:
        msg = f"card must be between 1 and 104, got {card}"
        raise ValueError(msg)
    if card == 55:
        return 7
    if card % 11 == 0:
        return 5
    if card % 10 == 0:
        return 3
    if card % 5 == 0:
        return 2
    return 1


def full_deck() -> list[int]:
    return list(range(1, 105))


def hand_seed_for(match_seed: int, hand_number: int) -> int:
    digest = hashlib.sha256(f"{match_seed}:{hand_number}".encode()).digest()[:8]
    return int.from_bytes(digest, "big")


def shuffled_deck(match_seed: int, hand_number: int) -> list[int]:
    deck = full_deck()
    # Deterministic by design: every participant must reproduce this exact
    # order from the seed, so a seeded PRNG is required, not a secure one.
    random.Random(hand_seed_for(match_seed, hand_number)).shuffle(deck)  # noqa: S311
    return deck


def deal(deck: list[int], player_ids: list[str]) -> tuple[dict[str, list[int]], list[int], list[int]]:
    """Deal round-robin in seating order, one card at a time, for 10 passes.

    Returns (hands in deal order, 4 row starts, undealt remainder).
    """
    player_count = len(player_ids)
    hands: dict[str, list[int]] = {player_id: [] for player_id in player_ids}
    for pass_number in range(10):
        for position, player_id in enumerate(player_ids):
            hands[player_id].append(deck[pass_number * player_count + position])
    row_start = 10 * player_count
    row_starts = deck[row_start : row_start + 4]
    remainder = deck[row_start + 4 :]
    return hands, row_starts, remainder
