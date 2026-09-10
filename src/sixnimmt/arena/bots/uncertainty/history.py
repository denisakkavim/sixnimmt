"""Accumulate public plays once, retaining their pre-selection boards."""

from dataclasses import dataclass, field

from sixnimmt.engine.views import MatchView, PlayHistoryView, RowView


class InferenceError(ValueError):
    """Observed history cannot support a consistent inference request."""


@dataclass(frozen=True)
class ObservedTurn:
    rows: tuple[RowView, ...]
    choices: tuple[tuple[str, int], ...]


@dataclass
class HandHistory:
    initial_rows: tuple[RowView, ...]
    own_initial_hand: tuple[int, ...]
    rows: tuple[RowView, ...]
    turns: list[ObservedTurn] = field(default_factory=list)
    records: list[PlayHistoryView] = field(default_factory=list)

    def add(self, record: PlayHistoryView, player_ids: tuple[str, ...]) -> None:
        if record.play_number <= len(self.records):
            if self.records[record.play_number - 1] != record:
                msg = "public play history changed after completion"
                raise InferenceError(msg)
            return
        if record.play_number != len(self.records) + 1:
            msg = "missing public play history"
            raise InferenceError(msg)
        if sorted(card.player_id for card in record.cards) != sorted(player_ids):
            msg = "public reveal does not contain every player"
            raise InferenceError(msg)
        turn = ObservedTurn(self.rows, tuple((card.player_id, card.card) for card in record.cards))
        rows = list(self.rows)
        # Fold recorded public placements, including the actual row choices.
        for card in sorted(record.cards, key=lambda item: item.card):
            index = next((i for i, row in enumerate(rows) if row.index == card.row_index), None)
            if index is None:
                msg = "public placement has no matching row"
                raise InferenceError(msg)
            row = rows[index]
            if card.captured:
                if card.captured != row.cards:
                    msg = "capture disagrees with public board"
                    raise InferenceError(msg)
                cards = (card.card,)
            else:
                cards = (*row.cards, card.card)
            rows[index] = RowView(index=row.index, cards=cards)
        self.turns.append(turn)
        self.rows = tuple(rows)
        self.records.append(record)


class PublicHistory:
    def __init__(self) -> None:
        self.hands: dict[int, HandHistory] = {}
        self.player_ids: tuple[str, ...] = ()
        self.own_id = ""
        self.match_id: str | None = None

    def observe(self, view: MatchView) -> None:
        if self.match_id is None:
            self.match_id = view.match_id
            self.own_id = view.you.player_id
            self.player_ids = (view.you.player_id, *(player.player_id for player in view.players))
        if self.match_id != view.match_id:
            msg = "learning state cannot be reused across matches"
            raise InferenceError(msg)
        for record in sorted(view.play_history, key=lambda item: (item.hand_number, item.play_number)):
            if not all(card.row_index is not None for card in record.cards):
                continue
            hand = self.hands.get(record.hand_number)
            if hand is None:
                msg = "missing initial observation for historical hand"
                raise InferenceError(msg)
            hand.add(record, self.player_ids)
        if view.hand_number not in self.hands:
            if view.play_number != 1 or view.you.selection is not None or len(view.you.hand) != 10:
                msg = "learning must start at the beginning of a hand"
                raise InferenceError(msg)
            if any(len(hand.turns) != 10 for hand in self.hands.values()):
                msg = "previous hand history is incomplete"
                raise InferenceError(msg)
            self.hands[view.hand_number] = HandHistory(view.rows, view.you.hand, view.rows)

    def current(self, view: MatchView) -> HandHistory:
        hand = self.hands[view.hand_number]
        if len(hand.turns) != view.play_number - 1 or hand.rows != view.rows:
            msg = "pre-selection board or history is incomplete"
            raise InferenceError(msg)
        return hand
