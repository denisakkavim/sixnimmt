"""Rebuild authoritative match state from a match log.

Replay folds the events themselves; it never re-runs the engine and never
touches a seed. That is the point of it: if the engine ever changed state
without emitting an event to say so, the replayed state and the live state
disagree, and the test that compares them fails. Everything downstream — the
UI, the analytics, every player view — rests on the log being a complete record,
and this is what holds it to that.

The input is a whole match log, admin events included. Folding a filtered
stream instead gives a viewer's view, which is `fold.build_view`'s job, not
this one.

Action counts are assigned from private count events. Older logs without those
markers retain their selection-only counting semantics; mixed writer versions
are not supported because matches cannot be resumed by another application version.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from sixnimmt.engine.cards import full_deck
from sixnimmt.engine.events import Event
from sixnimmt.engine.state import (
    MatchState,
    Phase,
    PlayerState,
    ResolutionState,
    RowState,
)


@dataclass
class _Seat:
    """One player, rebuilt from what the log says happened to them."""

    player_id: str
    display_name: str = ""
    agent_metadata: dict = field(default_factory=dict)
    hand: list[int] = field(default_factory=list)
    selection: int | None = None
    committed: bool = False
    penalty_cards: list[int] = field(default_factory=list)
    score_this_hand: int = 0
    total_score: int = 0
    actions_taken_this_play: int = 0

    def start_play(self) -> None:
        self.selection = None
        self.committed = False
        self.actions_taken_this_play = 0

    def start_hand(self) -> None:
        self.start_play()
        self.hand = []
        self.penalty_cards = []
        self.score_this_hand = 0


@dataclass
class _Replay:
    explicit_counts: bool = False
    match_id: str = ""
    phase: Phase = Phase.SETUP
    hand_number: int = 1
    play_number: int = 1
    match_seed: int | None = None
    seats: dict[str, _Seat] = field(default_factory=dict)
    rows: list[list[int]] = field(default_factory=list)
    resolution: ResolutionState | None = None
    revealed_this_hand: list[tuple[int, ...]] = field(default_factory=list)
    dealt_this_hand: list[int] = field(default_factory=list)
    undealt_remainder: list[int] = field(default_factory=list)
    winners: list[str] = field(default_factory=list)
    abandoned: bool = False


@dataclass(frozen=True)
class ReplayedMatch:
    """What a log folds into: the match state, and how the match ended."""

    state: MatchState
    winners: tuple[str, ...]
    abandoned: bool

    @property
    def status(self) -> str:
        if self.abandoned:
            return "abandoned"
        if self.state.phase == Phase.FINISHED:
            return "finished"
        if self.state.phase == Phase.SETUP:
            return "pending"
        return "in_progress"


def _seat(replay: _Replay, player_id: str) -> _Seat:
    if player_id not in replay.seats:
        replay.seats[player_id] = _Seat(player_id=player_id)
    return replay.seats[player_id]


def _apply_match_created(replay: _Replay, event: Event) -> None:
    """Seat the players; the admin copy is the one that carries their metadata.

    Both copies name the same seats in the same order, so either establishes
    seating. Only the admin copy carries `agent_metadata` and the display name
    as it was supplied, so it is allowed to overwrite what the public one set.
    """
    for entry in event.data.get("players", []):
        seat = _seat(replay, entry["player_id"])
        if event.audience != "admin":
            continue
        seat.display_name = entry.get("display_name", "")
        seat.agent_metadata = entry.get("agent_metadata", {})


def _start_hand(replay: _Replay, hand_number: int) -> None:
    replay.hand_number = hand_number
    replay.revealed_this_hand = []
    replay.dealt_this_hand = []
    replay.resolution = None
    for seat in replay.seats.values():
        seat.start_hand()


def _start_play(replay: _Replay, play_number: int) -> None:
    replay.phase = Phase.SELECTING
    replay.play_number = play_number
    replay.resolution = None
    for seat in replay.seats.values():
        seat.start_play()


def _initialise_rows(replay: _Replay, row_starts: list[list[int]]) -> None:
    replay.rows = [list(cards) for cards in row_starts]
    # The deal records which cards went to hands and rows but never the order of
    # the ones it did not use, so the remainder is rebuilt in ascending order.
    # It exists to show no card was lost, and nothing depends on its order.
    used = set(replay.dealt_this_hand) | {card for cards in row_starts for card in cards}
    replay.undealt_remainder = sorted(card for card in full_deck() if card not in used)


def _reveal(replay: _Replay, selections: dict[str, int]) -> None:
    """Every player's card comes up, hands shrink, and resolution begins."""
    replay.phase = Phase.RESOLVING
    replay.revealed_this_hand.append(tuple(sorted(selections.values())))
    ordered = tuple(sorted((card, player_id) for player_id, card in selections.items()))
    scores_before_play = tuple((seat.player_id, seat.score_this_hand) for seat in replay.seats.values())
    for player_id, card in selections.items():
        seat = _seat(replay, player_id)
        seat.hand.remove(card)
        seat.selection = None
        seat.committed = False
    replay.resolution = ResolutionState(ordered_cards=ordered, scores_before_play=scores_before_play)


def _place_card(replay: _Replay, row_index: int, row_cards: list[int]) -> None:
    replay.rows[row_index] = list(row_cards)
    if replay.resolution is None:
        msg = "a card was placed with no resolution in progress"
        raise ValueError(msg)
    # One `card_placed` per card, whether it fitted, capped a row, or was
    # chosen by a player, so this is the one place resolution advances.
    replay.resolution = replay.resolution.model_copy(update={"next_index": replay.resolution.next_index + 1})


def _take_row(replay: _Replay, player_id: str, captured: list[int], heads: int) -> None:
    seat = _seat(replay, player_id)
    seat.penalty_cards.extend(captured)
    seat.score_this_hand += heads


def _end_hand(replay: _Replay, totals: dict[str, int]) -> None:
    replay.resolution = None
    for player_id, total in totals.items():
        seat = _seat(replay, player_id)
        seat.total_score = total
        seat.score_this_hand = 0
        seat.penalty_cards = []
        seat.start_play()


def _apply(replay: _Replay, event: Event) -> None:  # noqa: C901
    data = event.data
    match event.type:
        case "match_created":
            replay.match_id = event.match_id
            _apply_match_created(replay, event)
        case "match_seed_assigned":
            replay.match_seed = data["match_seed"]
        case "hand_started":
            _start_hand(replay, data["hand_number"])
        case "cards_dealt":
            seat = _seat(replay, data["player_id"])
            seat.hand = list(data["hand"])
            replay.dealt_this_hand.extend(data["hand"])
        case "rows_initialised":
            _initialise_rows(replay, data["rows"])
        case "play_started":
            _start_play(replay, data["play"])
        case "selection_made":
            seat = _seat(replay, data["player_id"])
            seat.selection = data["card"]
            if not replay.explicit_counts:
                seat.actions_taken_this_play += 1
        case "action_counted":
            _seat(replay, data["player_id"]).actions_taken_this_play = data["actions_taken_this_play"]
        case "selection_cleared":
            _seat(replay, data["player_id"]).selection = None
        case "player_committed":
            _seat(replay, data["player_id"]).committed = True
        case "player_uncommitted":
            _seat(replay, data["player_id"]).committed = False
        case "cards_revealed":
            _reveal(replay, data["selections"])
        case "card_placed":
            _place_card(replay, data["row"], data["row_cards"])
        case "row_taken":
            _take_row(replay, data["player_id"], data["captured"], data["heads"])
        case "row_choice_required":
            replay.phase = Phase.AWAITING_ROW_CHOICE
            if replay.resolution is not None:
                replay.resolution = replay.resolution.model_copy(update={"awaiting_player": data["player_id"]})
        case "row_choice_made":
            replay.phase = Phase.RESOLVING
            if replay.resolution is not None:
                replay.resolution = replay.resolution.model_copy(update={"awaiting_player": None})
        case "hand_ended":
            _end_hand(replay, data["totals"])
        case "match_ended":
            replay.phase = Phase.FINISHED
            replay.winners = list(data["winners"])
        case "match_abandoned":
            # An application lifecycle event, not a game one: it ends the match
            # without moving it through a phase the engine would recognise.
            replay.abandoned = True
        case _:
            # Seeds for the hand, messages, and rejected actions change no
            # state. A rejection in particular must fold to nothing at all.
            pass


def _to_state(replay: _Replay) -> MatchState:
    players = tuple(
        PlayerState(
            player_id=seat.player_id,
            display_name=seat.display_name,
            agent_metadata=seat.agent_metadata,
            hand=tuple(seat.hand),
            selection=seat.selection,
            committed=seat.committed,
            penalty_cards=tuple(seat.penalty_cards),
            score_this_hand=seat.score_this_hand,
            total_score=seat.total_score,
            actions_taken_this_play=seat.actions_taken_this_play,
        )
        for seat in replay.seats.values()
    )
    return MatchState(
        match_id=replay.match_id,
        phase=replay.phase,
        players=players,
        rows=tuple(RowState(index=index, cards=tuple(cards)) for index, cards in enumerate(replay.rows)),
        hand_number=replay.hand_number,
        play_number=replay.play_number,
        resolution=replay.resolution,
        match_seed=replay.match_seed,
        undealt_remainder=tuple(replay.undealt_remainder),
        revealed_this_hand=tuple(replay.revealed_this_hand),
    )


def replay_events(events: Sequence[Event]) -> ReplayedMatch:
    """Fold a whole match log back into the state that produced it."""
    replay = _Replay(explicit_counts=any(event.type == "action_counted" for event in events))
    for event in events:
        _apply(replay, event)
    return ReplayedMatch(
        state=_to_state(replay),
        winners=tuple(replay.winners),
        abandoned=replay.abandoned,
    )
