"""Build a viewer's match view by folding only the events they may see.

This is the sole projection. Nothing here ever reads authoritative state, so a
fact a viewer was not sent cannot appear in their view: the leak is prevented by
the filter upstream rather than by remembering to blank a field here.
"""

import hashlib
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from sixnimmt_server.engine.audience import Viewer, addressed_player, visible_events
from sixnimmt_server.engine.cards import bull_heads
from sixnimmt_server.engine.events import Event
from sixnimmt_server.engine.rules import MatchProtocol
from sixnimmt_server.engine.state import Phase
from sixnimmt_server.engine.views import (
    MatchView,
    MessageView,
    OpponentView,
    PlayerSelfView,
    PrivateMessageView,
    RowView,
    ViewRole,
)

CARDS_PER_HAND = 10
MAX_VIEW_MESSAGES = 100


@dataclass
class _Seat:
    """What the fold knows about one player, from this viewer's stream alone."""

    player_id: str
    display_name: str = ""
    cards_in_hand: int = 0
    has_selection: bool = False
    committed: bool = False
    penalty_cards: list[int] = field(default_factory=list)
    total_score: int = 0

    @property
    def score_this_hand(self) -> int:
        return sum(bull_heads(card) for card in self.penalty_cards)


@dataclass
class _Fold:
    match_id: str = ""
    status: str = "pending"
    phase: Phase = Phase.SETUP
    hand_number: int = 1
    play_number: int = 1
    target_score: int = 66
    protocol: MatchProtocol = field(default_factory=MatchProtocol)
    negotiation_enabled: bool = False
    anonymise_display_names: bool = False
    max_actions_per_play: int | None = None
    seats: dict[str, _Seat] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    rows: list[list[int]] = field(default_factory=list)
    own_hand: list[int] = field(default_factory=list)
    own_selection: int | None = None
    own_actions: int = 0
    own_remaining: int | None = None
    explicit_counts: bool = False
    messages: deque[MessageView | PrivateMessageView] = field(default_factory=lambda: deque(maxlen=MAX_VIEW_MESSAGES))
    message_count: int = 0
    revealed_this_hand: list[tuple[int, ...]] = field(default_factory=list)
    awaiting: str | None = None
    awaiting_card: int | None = None
    winners: list[str] = field(default_factory=list)


def _seat(state: _Fold, player_id: str) -> _Seat:
    if player_id not in state.seats:
        state.seats[player_id] = _Seat(player_id=player_id)
        state.order.append(player_id)
    return state.seats[player_id]


def _apply_match_created(state: _Fold, data: dict) -> None:
    rules = data.get("rules", {})
    protocol = data.get("protocol", {})
    state.protocol = MatchProtocol.model_validate(protocol)
    state.target_score = rules.get("target_score", state.target_score)
    state.negotiation_enabled = protocol.get("negotiation_enabled", False)
    state.anonymise_display_names = protocol.get("anonymise_display_names", False)
    state.max_actions_per_play = protocol.get("max_actions_per_play")
    for entry in data.get("players", []):
        seat = _seat(state, entry["player_id"])
        seat.display_name = entry.get("display_name") or entry["player_id"]


def _start_hand(state: _Fold, hand_number: int) -> None:
    state.hand_number = hand_number
    state.revealed_this_hand = []
    state.own_hand = []
    state.own_selection = None
    for seat in state.seats.values():
        seat.penalty_cards = []
        seat.cards_in_hand = CARDS_PER_HAND
        seat.has_selection = False
        seat.committed = False


def _start_play(state: _Fold, play_number: int) -> None:
    state.phase = Phase.SELECTING
    state.play_number = play_number
    state.own_selection = None
    state.own_actions = 0
    state.own_remaining = state.max_actions_per_play
    state.awaiting = None
    state.awaiting_card = None
    state.messages.clear()
    state.message_count = 0
    for seat in state.seats.values():
        seat.has_selection = False
        seat.committed = False


def _bank_hand(state: _Fold, totals: dict[str, int]) -> None:
    """Bank the hand exactly as the engine does: totals up, everything else down.

    The piles empty so a folded score never carries a finished hand into the
    next one, and the per-play fields go with them. A later hand would reset
    those anyway, but the last hand of a match is followed by nothing, and a
    finished view must not still be reporting the final play's selection.
    """
    state.own_selection = None
    state.own_actions = 0
    state.own_remaining = state.max_actions_per_play
    for player_id, total in totals.items():
        seat = _seat(state, player_id)
        seat.total_score = total
        seat.penalty_cards = []
        seat.has_selection = False
        seat.committed = False


def _apply_reveal(state: _Fold, data: dict) -> None:
    selections = data["selections"]
    state.phase = Phase.RESOLVING
    state.revealed_this_hand.append(tuple(sorted(selections.values())))
    for player_id, card in selections.items():
        seat = _seat(state, player_id)
        seat.cards_in_hand = max(0, seat.cards_in_hand - 1)
        seat.has_selection = False
        seat.committed = False
        if card in state.own_hand:
            state.own_hand.remove(card)
    state.own_selection = None


def _apply_message(state: _Fold, event: Event, viewer: Viewer) -> None:
    data = event.data
    omniscient = viewer.role in (ViewRole.ADMIN, ViewRole.OMNISCIENT_OBSERVER)
    entry: MessageView | PrivateMessageView
    if event.type == "message_sent":
        if data["visibility"] == "direct":
            # Self-DMs are rejected by the engine so only one copy matches.
            recipient = data["from"] if omniscient else viewer.player_id
            if addressed_player(event) != recipient:
                return
        entry = MessageView(
            from_player=data["from"], to_player=data.get("to"), visibility=data["visibility"], body=data["body"]
        )
    else:
        if omniscient or (viewer.role == ViewRole.PLAYER and viewer.player_id in (data["from"], data["to"])):
            return
        entry = PrivateMessageView(from_player=data["from"], to_player=data["to"])
    state.messages.append(entry)
    state.message_count += 1


def _apply(state: _Fold, event: Event, viewer: Viewer) -> None:  # noqa: C901
    data = event.data
    match event.type:
        case "match_created":
            state.match_id = event.match_id
            _apply_match_created(state, data)
        case "match_started":
            state.status = "in_progress"
        case "hand_started":
            _start_hand(state, data["hand_number"])
        case "cards_dealt":
            state.own_hand = list(data["hand"])
            _seat(state, data["player_id"]).cards_in_hand = len(data["hand"])
        case "rows_initialised":
            state.rows = [list(row) for row in data["rows"]]
        case "play_started":
            _start_play(state, data["play"])
        case "selection_made":
            state.own_selection = data["card"]
            if not state.explicit_counts:
                state.own_actions += 1
        case "action_counted":
            if data["player_id"] == viewer.player_id:
                state.own_actions = data["actions_taken_this_play"]
                state.own_remaining = data["actions_remaining_this_play"]
        case "message_sent" | "private_message_occurred":
            _apply_message(state, event, viewer)
        case "selection_registered":
            _seat(state, data["player_id"]).has_selection = True
        case "selection_cleared":
            _seat(state, data["player_id"]).has_selection = False
            # A replacement re-sets this from the selection_made that follows;
            # an uncommit does not, and must not leave a stale card behind.
            if data["player_id"] == viewer.player_id:
                state.own_selection = None
        case "player_committed":
            _seat(state, data["player_id"]).committed = True
        case "player_uncommitted":
            _seat(state, data["player_id"]).committed = False
        case "cards_revealed":
            _apply_reveal(state, data)
        case "card_placed":
            state.rows[data["row"]] = list(data["row_cards"])
        case "row_taken":
            _seat(state, data["player_id"]).penalty_cards.extend(data["captured"])
        case "row_choice_required":
            state.phase = Phase.AWAITING_ROW_CHOICE
            state.awaiting = data["player_id"]
            state.awaiting_card = data["card"]
        case "row_choice_made":
            state.phase = Phase.RESOLVING
            state.awaiting = None
            state.awaiting_card = None
        case "hand_ended":
            _bank_hand(state, data["totals"])
        case "match_ended":
            state.status = "finished"
            state.phase = Phase.FINISHED
            state.winners = list(data["winners"])
        case "match_abandoned":
            state.status = "abandoned"
            state.phase = Phase.FINISHED
        case _:
            pass


def _legal_actions(state: _Fold, viewer: Viewer) -> tuple[str, ...]:
    """Advisory only. The engine revalidates every action regardless."""
    if viewer.role != ViewRole.PLAYER:
        return ()
    if state.phase == Phase.AWAITING_ROW_CHOICE:
        return ("choose_row",) if state.awaiting == viewer.player_id else ()
    if state.phase != Phase.SELECTING:
        return ()
    seat = state.seats.get(viewer.player_id or "")
    if seat is None:
        return ()
    if state.max_actions_per_play is not None and state.own_actions >= state.max_actions_per_play:
        return ()
    actions = ["select_card"]
    if state.negotiation_enabled:
        if state.own_selection is not None and not seat.committed:
            actions.append("commit")
        if seat.committed and not all(other.committed for other in state.seats.values()):
            actions.append("uncommit")
        actions.append("send_message")
    return tuple(actions)


def _displayed_name(state: _Fold, seat: _Seat, viewer: Viewer) -> str:
    """The name this viewer is entitled to see for an opponent.

    Anonymising hides who an agent is facing so it cannot condition its play on
    the opponent's identity. Omniscient observers and admin keep real names:
    they drive the development UI and the log, which exist to be readable.
    """
    anonymous_roles = (ViewRole.PLAYER, ViewRole.PUBLIC_SPECTATOR)
    if not state.anonymise_display_names or viewer.role not in anonymous_roles:
        return seat.display_name or seat.player_id
    seat_number = state.order.index(seat.player_id) + 1
    return f"Player {seat_number}"


def _view_id(viewer: Viewer, view_version: int) -> str:
    """Opaque, derived only from the caller's own identity and cursor.

    Never from the global seq, a global version, or a timestamp: a view_id that
    encodes global state reintroduces exactly the leak the cursor closes.
    """
    material = f"{viewer.role.value}:{viewer.player_id or ''}:{view_version}"
    return "v_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def _self_view(state: _Fold, viewer: Viewer) -> PlayerSelfView:
    seat = state.seats.get(viewer.player_id or "") if viewer.player_id else None
    remaining = state.own_remaining
    if not state.explicit_counts and state.max_actions_per_play is not None:
        remaining = max(0, state.max_actions_per_play - state.own_actions)
    return PlayerSelfView(
        player_id=viewer.player_id or "",
        hand=tuple(state.own_hand),
        selection=state.own_selection,
        committed=seat.committed if seat else False,
        penalty_cards=tuple(seat.penalty_cards) if seat else (),
        score_this_hand=seat.score_this_hand if seat else 0,
        total_score=seat.total_score if seat else 0,
        actions_taken_this_play=state.own_actions,
        actions_remaining_this_play=remaining,
    )


class ViewFolder:
    """Incrementally fold a viewer's stream without retaining hidden events."""

    def __init__(self, viewer: Viewer, *, explicit_counts: bool = True) -> None:
        # Live writers emit count markers; legacy whole-log readers choose below.
        self._viewer = viewer
        self._state = _Fold(explicit_counts=explicit_counts)
        self._version = 0

    def apply(self, events: Sequence[Event]) -> None:
        for event in visible_events(events, self._viewer):
            _apply(self._state, event, self._viewer)
            self._version += 1

    def view(self) -> MatchView:
        return _project(self._state, self._viewer, self._version)


def build_view(events: Sequence[Event], viewer: Viewer) -> MatchView:
    """Read current or legacy logs through the same fold used by live matches."""
    visible = visible_events(events, viewer)
    folder = ViewFolder(viewer, explicit_counts=any(event.type == "action_counted" for event in visible))
    folder.apply(visible)
    return folder.view()


def _project(state: _Fold, viewer: Viewer, version: int) -> MatchView:
    others = tuple(
        OpponentView(
            player_id=seat.player_id,
            display_name=_displayed_name(state, seat, viewer),
            cards_in_hand=seat.cards_in_hand,
            has_selection=seat.has_selection,
            committed=seat.committed,
            penalty_cards=tuple(seat.penalty_cards),
            score_this_hand=seat.score_this_hand,
            total_score=seat.total_score,
        )
        for player_id in state.order
        for seat in [state.seats[player_id]]
        if player_id != viewer.player_id
    )
    return MatchView(
        match_id=state.match_id,
        view_version=version,
        view_id=_view_id(viewer, version),
        status=state.status,
        phase=state.phase,
        hand_number=state.hand_number,
        play_number=state.play_number,
        you=_self_view(state, viewer),
        rows=tuple(RowView(index=index, cards=tuple(cards)) for index, cards in enumerate(state.rows)),
        players=others,
        revealed_this_hand=tuple(state.revealed_this_hand),
        awaiting=state.awaiting,
        awaiting_card=state.awaiting_card,
        legal_actions=_legal_actions(state, viewer),
        target_score=state.target_score,
        protocol=state.protocol,
        messages=tuple(entry for entry in state.messages if isinstance(entry, MessageView)),
        private_messages_observed=tuple(entry for entry in state.messages if isinstance(entry, PrivateMessageView)),
        messages_omitted=state.message_count - len(state.messages),
    )
