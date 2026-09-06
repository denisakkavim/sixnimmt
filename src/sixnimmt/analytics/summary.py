"""Surface-independent scores, action counts, messaging and measured latency."""

from collections.abc import Sequence

from pydantic import BaseModel, Field

from sixnimmt.engine.audience import addressed_player
from sixnimmt.engine.events import Event
from sixnimmt.engine.replay import replay_events
from sixnimmt.persistence.manifest import ManifestMatch
from sixnimmt.persistence.sink import ActionRecord


class SeatSummary(BaseModel):
    player_id: str
    total_score: int
    score_this_hand: int
    hand_scores: dict[int, int] = Field(default_factory=dict)
    won: bool
    tied: bool
    actions_attempted: int = 0
    actions_rejected: int = 0
    messages_sent: int = 0
    messages_received: int = 0
    decision_latencies_ms: list[float] | None = None


class MatchSummary(BaseModel):
    match_id: str
    outcome: str
    winners: tuple[str, ...]
    ended_by: str | None = None
    reason: str | None = None
    has_manifest: bool
    notes: tuple[str, ...] = ()
    players: tuple[SeatSummary, ...]


def summarise(
    events: Sequence[Event], actions: Sequence[ActionRecord], manifest_entry: ManifestMatch | None = None
) -> MatchSummary:
    """Derive metrics from either writer; unavailable latency stays unavailable."""
    if manifest_entry is not None and manifest_entry.manifest_version != 1:
        msg = f"unsupported manifest version {manifest_entry.manifest_version}"
        raise ValueError(msg)
    replayed = replay_events(events)
    state = replayed.state
    if manifest_entry is not None and manifest_entry.match_id != state.match_id:
        msg = "manifest entry belongs to a different match"
        raise ValueError(msg)
    outcome = manifest_entry.outcome if manifest_entry is not None else replayed.status
    winners = replayed.winners if outcome == "finished" else ()
    seats = {
        player.player_id: SeatSummary(
            player_id=player.player_id,
            total_score=player.total_score,
            score_this_hand=player.score_this_hand,
            won=player.player_id in winners and len(winners) == 1,
            tied=player.player_id in winners and len(winners) > 1,
        )
        for player in state.players
    }
    _count_actions(seats, events, actions)
    _count_events(seats, events)
    notes = ()
    if manifest_entry is None:
        notes = (
            ("No manifest supplied; abandonment reason and responsible seat are unknown.",)
            if outcome == "abandoned"
            else ("No manifest supplied.",)
        )
    return MatchSummary(
        match_id=state.match_id,
        outcome=outcome,
        winners=winners,
        ended_by=manifest_entry.ended_by if manifest_entry is not None else None,
        reason=manifest_entry.reason if manifest_entry is not None else None,
        has_manifest=manifest_entry is not None,
        notes=notes,
        players=tuple(seats.values()),
    )


def _count_actions(seats: dict[str, SeatSummary], events: Sequence[Event], actions: Sequence[ActionRecord]) -> None:
    # Old writers did not store outcomes, so private refusal events also identify
    # rejected records. Their sequence is canonical on both surfaces.
    rejected_sequences = {event.server_action_seq for event in events if event.type == "action_rejected"}
    for record in actions:
        seat = seats[record.player_id]
        seat.actions_attempted += 1
        if record.outcome == "rejected" or record.server_action_seq in rejected_sequences:
            seat.actions_rejected += 1
        if record.decision_duration_ms is not None:
            if seat.decision_latencies_ms is None:
                seat.decision_latencies_ms = []
            seat.decision_latencies_ms.append(record.decision_duration_ms)


def _count_events(seats: dict[str, SeatSummary], events: Sequence[Event]) -> None:
    previous_scores = dict.fromkeys(seats, 0)
    for event in events:
        if event.type == "hand_ended":
            for player_id, total in event.data["totals"].items():
                seats[player_id].hand_scores[event.hand] = total - previous_scores[player_id]
                previous_scores[player_id] = total
        if event.type != "message_sent":
            continue
        data = event.data
        # Count the sender copy of a direct message. This also works for engine
        # batches that have not yet been assigned action sequence numbers.
        if data["visibility"] == "direct" and addressed_player(event) != data["from"]:
            continue
        seats[data["from"]].messages_sent += 1
        recipients = [data["to"]] if data["visibility"] == "direct" else [pid for pid in seats if pid != data["from"]]
        for recipient in recipients:
            seats[recipient].messages_received += 1
