"""Bull head values and deck totals."""

import pytest

from sixnimmt.engine.cards import bull_heads, full_deck

# Independent lookup of known values: written by hand, not derived from the
# implementation, so the test cannot repeat an implementation bug.
HEAD_TABLE: dict[int, int] = {
    55: 7,
    11: 5,
    22: 5,
    33: 5,
    44: 5,
    66: 5,
    77: 5,
    88: 5,
    99: 5,
    10: 3,
    50: 3,
    100: 3,
    5: 2,
    15: 2,
    25: 2,
    95: 2,
    7: 1,
    23: 1,
    41: 1,
    103: 1,
}


@pytest.mark.parametrize(("card", "expected"), sorted(HEAD_TABLE.items()))
def test_bull_heads_match_literal_table(card: int, expected: int) -> None:
    assert bull_heads(card) == expected


@pytest.mark.parametrize("card", [44, 66, 88, 99])
def test_multiples_of_11_score_5_despite_other_rules(card: int) -> None:
    assert bull_heads(card) == 5


def test_deck_has_104_distinct_cards_totalling_171_heads() -> None:
    deck = full_deck()

    assert len(deck) == 104
    assert len(set(deck)) == 104
    assert sum(bull_heads(card) for card in deck) == 171


def test_bull_head_groups_have_expected_sizes_and_values() -> None:
    deck = full_deck()
    heads = {card: bull_heads(card) for card in deck}

    card_55 = [card for card in deck if card == 55]
    other_11s = [card for card in deck if card != 55 and card % 11 == 0]
    tens = [card for card in deck if card % 10 == 0 and card % 11 != 0]
    other_5s = [card for card in deck if card % 5 == 0 and card % 10 != 0 and card % 11 != 0]
    rest = [card for card in deck if card % 5 != 0 and card % 11 != 0]

    assert (len(card_55), heads[55]) == (1, 7)
    assert len(other_11s) == 8
    assert all(heads[card] == 5 for card in other_11s)
    assert len(tens) == 10
    assert all(heads[card] == 3 for card in tens)
    assert len(other_5s) == 9
    assert all(heads[card] == 2 for card in other_5s)
    assert len(rest) == 76
    assert all(heads[card] == 1 for card in rest)

    subtotal = 7 + 8 * 5 + 10 * 3 + 9 * 2 + 76 * 1
    assert subtotal == 171


def test_bull_heads_rejects_cards_outside_1_to_104() -> None:
    with pytest.raises(ValueError, match="card must be between 1 and 104"):
        bull_heads(0)

    with pytest.raises(ValueError, match="card must be between 1 and 104"):
        bull_heads(105)
