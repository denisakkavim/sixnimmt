"""Validate intermediate events as well as live states over complete random matches."""

from collections import Counter
from itertools import pairwise

import pytest

from sixnimmt_server.arena.bots import RandomBot
from sixnimmt_server.arena.runner import derive_seed, observe_player, run_match
from sixnimmt_server.engine.cards import bull_heads
from sixnimmt_server.engine.events import Event
from sixnimmt_server.engine.state import MatchState, Phase


class MatchLedger:
    """Independent test ledger, not a persisted replay implementation."""

    def __init__(self, player_count: int) -> None:
        self.ids = [f"player_{seat + 1}" for seat in range(player_count)]
        self.hands: dict[str, list[int]] = {}
        self.piles: dict[str, list[int]] = {}
        self.totals = dict.fromkeys(self.ids, 0)
        self.rows: list[list[int]] = []
        self.pending: list[int] = []
        self.remainder: list[int] = []
        self.play_scores = dict.fromkeys(self.ids, 0)
        self.completed_plays = 0
        self.completed_hands = 0
        self.placements = 0
        self.captures = 0
        self.row_choices = 0
        self.winners: tuple[str, ...] = ()
        self.before_rows: list[list[int]] | None = None
        self.capture_row: int | None = None
        self.owners: dict[int, str] = {}
        self.finished = False

    def _check_cards(self) -> None:
        cards = [card for hand in self.hands.values() for card in hand]
        cards.extend(card for pile in self.piles.values() for card in pile)
        cards.extend(card for row in self.rows for card in row)
        cards.extend(self.pending)
        cards.extend(self.remainder)
        assert sorted(cards) == list(range(1, 105))
        assert sum(bull_heads(card) for card in cards) == 171

    def _check_rows(self) -> None:
        assert len(self.rows) == 4
        for row in self.rows:
            assert 1 <= len(row) <= 5
            assert all(left < right for left, right in pairwise(row))

    def _take_row(self, event: Event) -> None:
        data = event.data
        row = data["row"]
        captured = data["captured"]
        assert self.pending
        assert self.capture_row is None
        self.before_rows = [list(cards) for cards in self.rows]
        self.capture_row = row
        assert captured == self.rows[row]
        assert data["player_id"] == self.owners[self.pending[0]]
        assert data["heads"] == sum(bull_heads(card) for card in captured)
        eligible = [index for index, cards in enumerate(self.rows) if cards[-1] < self.pending[0]]
        if data["reason"] == "sixth_card":
            assert len(captured) == 5
            assert row == max(eligible, key=lambda index: self.rows[index][-1])
        else:
            assert data["reason"] == "too_low"
            assert eligible == []
        self.piles[data["player_id"]].extend(captured)
        self.rows[row] = []
        self.captures += 1
        # row_taken precedes card_placed; the replacement is still pending here.
        self._check_cards()

    def _place_card(self, event: Event) -> None:
        data = event.data
        card, row = data["card"], data["row"]
        assert card == self.pending[0]
        previous = self.before_rows if self.before_rows is not None else self.rows
        eligible = [index for index, cards in enumerate(previous) if cards[-1] < card]
        if eligible:
            assert row == max(eligible, key=lambda index: previous[index][-1])
        else:
            assert self.capture_row == row
        if self.capture_row is not None:
            assert row == self.capture_row
            assert data["row_cards"] == [card]
        else:
            assert len(previous[row]) < 5
            assert data["row_cards"] == [*previous[row], card]
        self.rows[row] = list(data["row_cards"])
        self.pending.pop(0)
        self.before_rows = None
        self.capture_row = None
        self.placements += 1
        self._check_rows()
        self._check_cards()

    def _end_hand(self, event: Event) -> None:
        assert self.completed_plays == 10
        assert all(hand == [] for hand in self.hands.values())
        assert self.pending == []
        self._check_cards()
        scores = {player: sum(bull_heads(card) for card in self.piles[player]) for player in self.ids}
        assert event.data["hand_scores"] == scores
        for player, score in scores.items():
            assert score >= 0
            self.totals[player] += score
        assert event.data["totals"] == self.totals
        self.completed_hands += 1

    def _deal(self, event: Event) -> None:
        data = event.data
        match event.type:
            case "hand_started":
                assert all(score < 66 for score in self.totals.values())
                assert event.hand == self.completed_hands + 1
                self.hands = {}
                self.piles = {player: [] for player in self.ids}
                self.pending = []
                self.completed_plays = 0
            case "cards_dealt":
                assert event.audience == f"player:{data['player_id']}"
                self.hands[data["player_id"]] = list(data["hand"])
                assert len(data["hand"]) == 10
            case "rows_initialised":
                self.rows = [list(row) for row in data["rows"]]
                used = {card for cards in self.hands.values() for card in cards}
                used.update(card for row in self.rows for card in row)
                self.remainder = sorted(set(range(1, 105)) - used)
                assert len(self.remainder) == 100 - 10 * len(self.ids)
                self._check_rows()
                self._check_cards()

    def _reveal(self, event: Event) -> None:
        assert self.pending == []
        selections = event.data["selections"]
        assert set(selections) == set(self.ids)
        self.owners = {card: player for player, card in selections.items()}
        self.pending = sorted(selections.values())
        for player, card in selections.items():
            self.hands[player].remove(card)
        self._check_cards()

    def _fold(self, event: Event) -> None:
        data = event.data
        match event.type:
            case "hand_started" | "cards_dealt" | "rows_initialised":
                self._deal(event)
            case "play_started":
                assert event.play == self.completed_plays + 1
                self.play_scores = {player: sum(bull_heads(card) for card in self.piles[player]) for player in self.ids}
            case "cards_revealed":
                self._reveal(event)
            case "row_taken":
                self._take_row(event)
            case "row_choice_required":
                assert event.audience == f"player:{data['player_id']}"
                assert data["card"] == self.pending[0]
                self.row_choices += 1
            case "card_placed":
                self._place_card(event)
            case "play_ended":
                assert self.pending == []
                penalties = {
                    player: sum(bull_heads(card) for card in self.piles[player]) - self.play_scores[player]
                    for player in self.ids
                }
                assert data["penalties"] == penalties
                assert all(value >= 0 for value in penalties.values())
                self.completed_plays += 1
                assert all(len(hand) == 10 - self.completed_plays for hand in self.hands.values())
            case "hand_ended":
                self._end_hand(event)
            case "match_ended":
                assert max(self.totals.values()) >= 66
                assert data["totals"] == self.totals
                self.winners = tuple(
                    sorted(player for player in self.ids if self.totals[player] == min(self.totals.values()))
                )
                assert tuple(data["winners"]) == self.winners
                self.finished = True

    def __call__(self, state: MatchState, events: tuple[Event, ...]) -> None:
        for event in events:
            self._fold(event)
        assert [list(row.cards) for row in state.rows] == self.rows
        assert Counter(state.undealt_remainder) == Counter(self.remainder)
        expected_hand = self.completed_hands if self.finished else self.completed_hands + 1
        assert state.hand_number == expected_hand
        for player in state.players:
            assert list(player.hand) == self.hands[player.player_id]
            assert player.total_score == self.totals[player.player_id]
            if self.finished:
                assert player.penalty_cards == ()
                assert player.score_this_hand == 0
            else:
                assert list(player.penalty_cards) == self.piles[player.player_id]
                assert player.score_this_hand == sum(bull_heads(card) for card in player.penalty_cards)
            observation = observe_player(state, player.player_id)
            assert observation.hand == player.hand
            visible = set(observation.hand)
            visible.update(card for row in observation.rows for card in row.cards)
            assert visible.isdisjoint(state.undealt_remainder)
            assert all(visible.isdisjoint(other.hand) for other in state.players if other.player_id != player.player_id)
        if state.resolution is not None:
            assert [card for card, _ in state.resolution.ordered_cards[state.resolution.next_index :]] == self.pending
        else:
            assert self.pending == []


def _validate_matches(player_count: int, games: int) -> None:
    for game in range(games):
        ledger = MatchLedger(player_count)
        seed = derive_seed(1234, "match", game)
        bots = [RandomBot(derive_seed(1234, "bot", game, seat)) for seat in range(player_count)]
        result = run_match(bots, seed, match_id=f"arena_{game}", observer=ledger)
        assert result.final_state.phase == Phase.FINISHED
        assert ledger.finished
        assert ledger.placements == ledger.completed_hands * 10 * player_count
        assert ledger.captures > 0
        assert result.winners == ledger.winners


@pytest.mark.parametrize("player_count", [2, 3, 5, 10])
def test_random_matches_preserve_intermediate_invariants(player_count: int) -> None:
    _validate_matches(player_count, 3)


@pytest.mark.arena_slow
@pytest.mark.parametrize("player_count", [2, 3, 5, 10])
def test_thousand_random_matches_preserve_intermediate_invariants(player_count: int) -> None:
    _validate_matches(player_count, 1000)
