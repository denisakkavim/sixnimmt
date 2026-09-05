"""The match registry and the per-match serialisation boundary.

Every state change for a match runs inside one lock, in the order §9.5 fixes.
Two steps in particular must stay inside it: the idempotency lookup, and the
`expected_view_version` check. Hoisting either out as an optimisation leaves a
window in which another player's action lands between the check and the
transition, which makes both guards advisory rather than real.
"""

import asyncio
import json
import secrets
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sixnimmt_server.engine.actions import (
    Action,
    ChooseRowAction,
    MessageVisibility,
    SendMessageAction,
    UncommitAction,
)
from sixnimmt_server.engine.audience import Viewer
from sixnimmt_server.engine.errors import EngineRejection
from sixnimmt_server.engine.events import (
    ActionRejectedEvent,
    Event,
    EventType,
    MatchAbandonedEvent,
    assign_sequence,
    audience_for_player,
)
from sixnimmt_server.engine.fold import build_view
from sixnimmt_server.engine.rules import GameRules, MatchProtocol, OnInvalidAction
from sixnimmt_server.engine.setup import open_match, start_match
from sixnimmt_server.engine.state import MatchState, Phase, PlayerSeat
from sixnimmt_server.engine.transition import transition
from sixnimmt_server.engine.views import MatchView, ViewRole
from sixnimmt_server.server.auth import MatchTokens, TokenRegistry
from sixnimmt_server.server.errors import ApiError, ApiErrorCode, api_code_for, match_not_found
from sixnimmt_server.server.sink import (
    ActionRecord,
    EventSink,
    JsonlEventSink,
    LiveEventStream,
    NullEventSink,
)

# Retained per player, never per match: a shared bound would let one player's
# traffic evict another's entries, and the change in dedup behaviour that
# follows is itself an oracle for how active that player has been.
IDEMPOTENCY_ENTRIES_PER_PLAYER = 1000


def _fingerprint(action: Action) -> str:
    """What makes two submissions the same action.

    `from_view` and `expected_view_version` are excluded: they are metadata a
    legitimate retry may vary, and varying them must still hit the cache.
    """
    payload = action.model_dump(mode="json", exclude={"action_id", "from_view", "expected_view_version"})
    return json.dumps(payload, sort_keys=True)


@dataclass
class CacheEntry:
    """One remembered outcome: the view it produced, or the refusal it raised."""

    fingerprint: str
    view: MatchView | None
    error: ApiError | None


class IdempotencyCache:
    """Results keyed `(player_id, action_id)`, bounded per player."""

    def __init__(self, entries_per_player: int = IDEMPOTENCY_ENTRIES_PER_PLAYER) -> None:
        self._entries_per_player = entries_per_player
        self._by_player: dict[str, OrderedDict[str, CacheEntry]] = {}

    def get(self, player_id: str, action_id: str) -> CacheEntry | None:
        return self._by_player.get(player_id, OrderedDict()).get(action_id)

    def put(self, player_id: str, action_id: str, entry: CacheEntry) -> None:
        entries = self._by_player.setdefault(player_id, OrderedDict())
        entries[action_id] = entry
        while len(entries) > self._entries_per_player:
            entries.popitem(last=False)


@dataclass
class MatchRecord:
    """One match: its state, its log, and the lock that serialises changes."""

    match_id: str
    state: MatchState
    rules: GameRules
    protocol: MatchProtocol
    # The live copy, which serves cursors and subscribers, and the durable one,
    # which the match can be rebuilt from long after this process is gone.
    stream: LiveEventStream = field(default_factory=LiveEventStream)
    sink: EventSink = field(default_factory=NullEventSink)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    idempotency: IdempotencyCache = field(default_factory=IdempotencyCache)
    action_records: list[ActionRecord] = field(default_factory=list)
    next_seq: int = 1
    next_server_action_seq: int = 1
    abandoned: bool = False

    @property
    def status(self) -> str:
        if self.abandoned:
            return "abandoned"
        if self.state.phase == Phase.SETUP:
            return "pending"
        if self.state.phase == Phase.FINISHED:
            return "finished"
        return "in_progress"

    def append(self, events: Sequence[Event], server_action_seq: int) -> None:
        numbered = assign_sequence(list(events), self.next_seq, server_action_seq)
        self.next_seq += len(numbered)
        self.stream.append(numbered)
        self.sink.append(numbered)

    def record_action(self, action_record: ActionRecord) -> None:
        """Keep the canonical processing order, in memory and on disk."""
        self.action_records.append(action_record)
        self.sink.record_action(action_record)

    def view_for(self, viewer: Viewer) -> MatchView:
        """Fold the caller's own filtered stream. The only projection there is."""
        return build_view(self.stream.visible_stream(viewer), viewer)


class MatchStore:
    """The registry. Owns match records, tokens, and the serialisation boundary."""

    def __init__(self, registry: TokenRegistry, log_directory: Path | None = None) -> None:
        self._registry = registry
        self._log_directory = log_directory
        self._matches: dict[str, MatchRecord] = {}

    @property
    def match_ids(self) -> list[str]:
        return list(self._matches)

    def record_for(self, match_id: str) -> MatchRecord | None:
        return self._matches.get(match_id)

    def authorise(self, token: str | None, match_id: str) -> tuple[MatchRecord, Viewer]:
        """Resolve a token against a match, or refuse indistinguishably.

        An unknown token, a token minted for another match, and a match that
        never existed all leave here by the same path with the same error, so
        no token can be used to probe which match IDs exist.
        """
        viewer = self._registry.viewer_for(token, match_id)
        record = self._matches.get(match_id)
        if viewer is None or record is None:
            raise match_not_found(match_id)
        return record, viewer

    def create(
        self,
        players: Sequence[PlayerSeat],
        seed: int | None = None,
        rules: GameRules | None = None,
        protocol: MatchProtocol | None = None,
    ) -> tuple[MatchRecord, MatchTokens, int]:
        """Open a match at SETUP and mint its tokens. The first hand waits for start."""
        match_seed = seed if seed is not None else secrets.randbits(63)
        match_id = f"m_{secrets.token_hex(8)}"
        rules = rules or GameRules()
        protocol = protocol or MatchProtocol()
        try:
            state, events = open_match(match_id, players, match_seed, rules, protocol)
        except EngineRejection as rejection:
            raise ApiError(api_code_for(rejection.code), str(rejection)) from rejection

        record = MatchRecord(
            match_id=match_id,
            state=state,
            rules=rules,
            protocol=protocol,
            sink=self._sink_for(match_id),
        )
        record.append(events, server_action_seq=0)
        self._matches[match_id] = record
        tokens = self._registry.mint_for_match(match_id, [seat.player_id for seat in players])
        return record, tokens, match_seed

    def _sink_for(self, match_id: str) -> EventSink:
        """A file-backed log when the server was given a directory, else none."""
        if self._log_directory is None:
            return NullEventSink()
        return JsonlEventSink(self._log_directory, match_id)

    async def start(self, record: MatchRecord) -> None:
        """Deal the first hand and open the first play."""
        async with record.lock:
            self._check_playable(record)
            if record.state.phase != Phase.SETUP:
                raise ApiError(ApiErrorCode.MATCH_ALREADY_STARTED, f"match {record.match_id} has already started")
            server_action_seq = self._next_action_seq(record)
            state, events = start_match(record.state)
            record.state = state
            record.append(events, server_action_seq)

    async def abandon(self, record: MatchRecord) -> None:
        """Append `match_abandoned`, then release the match's live resources.

        A server lifecycle concern, not a game rule, so it is emitted here and
        not by the engine. The log is retained: later actions must be able to
        answer MATCH_ABANDONED rather than pretending the match never existed.
        """
        async with record.lock:
            if record.abandoned:
                return
            server_action_seq = self._next_action_seq(record)
            abandoned: Event = MatchAbandonedEvent(
                match_id=record.match_id,
                hand=record.state.hand_number,
                play=record.state.play_number,
                audience="public",
                data={},
            )
            record.append([abandoned], server_action_seq)
            record.abandoned = True
            # Nothing can be applied to this match again, so the cached results
            # have nothing left to deduplicate.
            record.idempotency = IdempotencyCache()
        # Outside the lock: waiters wake to a closed stream and return
        # MATCH_ABANDONED rather than hanging until their timeout expires.
        record.stream.close()
        # The handles go with them; §11.4 keeps the log itself on disk.
        record.sink.close()

    async def apply_action(
        self,
        record: MatchRecord,
        viewer: Viewer,
        action: Action,
    ) -> MatchView:
        """Run one action through §9.5's ordered steps, all inside the lock."""
        player_id = self._acting_player(viewer)
        action_id = action.action_id or secrets.token_hex(16)

        async with record.lock:
            cached = record.idempotency.get(player_id, action_id)
            if cached is not None:
                return self._replay(cached, action)

            server_action_seq = self._next_action_seq(record)
            record.record_action(
                ActionRecord(
                    server_action_seq=server_action_seq,
                    action_id=action_id,
                    player_id=player_id,
                    type=action.type.value,
                    # Audit reference only: recorded, never validated, never a
                    # reason to reject. A stale value must cost the caller nothing.
                    from_view=action.from_view,
                    received_at=datetime.now(UTC),
                )
            )

            self._check_playable(record)
            self._check_expected_version(record, viewer, action)
            return self._apply_inside_boundary(record, viewer, player_id, action, action_id, server_action_seq)

    def _apply_inside_boundary(
        self,
        record: MatchRecord,
        viewer: Viewer,
        player_id: str,
        action: Action,
        action_id: str,
        server_action_seq: int,
    ) -> MatchView:
        try:
            self._check_message_envelope(record, action)
            state, events = transition(record.state, player_id, action, record.protocol, record.rules)
        except ApiError as refusal:
            self._reject(record, viewer, player_id, action_id, action, refusal, server_action_seq)
            raise
        except EngineRejection as rejection:
            refusal = self._refusal_for(record, player_id, action, rejection)
            self._reject(record, viewer, player_id, action_id, action, refusal, server_action_seq)
            raise refusal from rejection

        record.state = state
        record.append(events, server_action_seq)
        view = record.view_for(viewer)
        record.idempotency.put(player_id, action_id, CacheEntry(_fingerprint(action), view, None))
        return view

    def _reject(
        self,
        record: MatchRecord,
        viewer: Viewer,
        player_id: str,
        action_id: str,
        action: Action,
        refusal: ApiError,
        server_action_seq: int,
    ) -> None:
        """Record a refused action: private event, caller's own cursor, cached.

        `on_invalid_action` governs this path. Only `reject` exists today, and
        matching on it keeps a future value a visible gap rather than a silent
        change of behaviour.
        """
        match record.protocol.on_invalid_action:
            case OnInvalidAction.REJECT:
                rejected: Event = ActionRejectedEvent(
                    match_id=record.match_id,
                    hand=record.state.hand_number,
                    play=record.state.play_number,
                    audience=audience_for_player(player_id),
                    data={"code": refusal.code.value, "message": refusal.message, "action_type": action.type.value},
                )
                record.append([rejected], server_action_seq)

        view = record.view_for(viewer)
        refusal.legal_actions = view.legal_actions
        refusal.view_version = view.view_version
        # Cached because it emitted an event and moved the caller's cursor;
        # replaying it on retry is what stops one mistake being counted twice.
        record.idempotency.put(player_id, action_id, CacheEntry(_fingerprint(action), None, refusal))

    def _replay(self, cached: CacheEntry, action: Action) -> MatchView:
        if cached.fingerprint != _fingerprint(action):
            raise ApiError(
                ApiErrorCode.IDEMPOTENCY_KEY_REUSED,
                "this action_id was already used for a different action; use a new action_id",
            )
        if cached.error is not None:
            raise cached.error
        if cached.view is None:
            msg = "cached action result is missing both a view and an error"
            raise RuntimeError(msg)
        return cached.view

    def _next_action_seq(self, record: MatchRecord) -> int:
        server_action_seq = record.next_server_action_seq
        record.next_server_action_seq += 1
        return server_action_seq

    def _acting_player(self, viewer: Viewer) -> str:
        if viewer.role != ViewRole.PLAYER or viewer.player_id is None:
            raise ApiError(ApiErrorCode.NOT_AUTHORIZED, "only a player token may act in a match")
        return viewer.player_id

    def _check_playable(self, record: MatchRecord) -> None:
        if record.abandoned:
            raise ApiError(ApiErrorCode.MATCH_ABANDONED, f"match {record.match_id} has been abandoned")

    def _check_expected_version(self, record: MatchRecord, viewer: Viewer, action: Action) -> None:
        """Optimistic concurrency in the caller's own cursor, evaluated here.

        Inside the boundary and at the same linearization point as the
        transition, so nothing visible to the caller can land between the two.
        """
        expected = action.expected_view_version
        if expected is None:
            return
        current = record.stream.view_version(viewer)
        if expected == current:
            return
        raise ApiError(
            ApiErrorCode.VERSION_CONFLICT,
            f"you acted on view_version {expected} but your view is now at {current}; read the match again",
            view_version=current,
            legal_actions=record.view_for(viewer).legal_actions,
        )

    def _check_message_envelope(self, record: MatchRecord, action: Action) -> None:
        """Envelope checks a message must pass before the engine sees it.

        Only meaningful once negotiation is on; with messaging switched off the
        engine's NEGOTIATION_DISABLED is the truthful answer to any message.
        """
        if not isinstance(action, SendMessageAction) or not record.protocol.negotiation_enabled:
            return
        if len(action.body) > record.protocol.max_message_length:
            raise ApiError(
                ApiErrorCode.MESSAGE_TOO_LONG,
                f"your message is {len(action.body)} characters; the limit is {record.protocol.max_message_length}",
            )
        if action.visibility != MessageVisibility.DIRECT:
            return
        if not record.protocol.allow_direct_messages:
            raise ApiError(ApiErrorCode.DIRECT_MESSAGES_DISABLED, "this match allows table messages only")
        seated = [player.player_id for player in record.state.players]
        if action.to_player not in seated:
            raise ApiError(ApiErrorCode.RECIPIENT_NOT_FOUND, f"there is no player {action.to_player} in this match")

    def _refusal_for(self, record: MatchRecord, player_id: str, action: Action, rejection: EngineRejection) -> ApiError:
        code = api_code_for(rejection.code)
        if record.state.phase == Phase.SETUP:
            return ApiError(ApiErrorCode.MATCH_NOT_STARTED, f"match {record.match_id} has not been started yet")
        if isinstance(action, ChooseRowAction):
            code = self._refine_row_choice(record, player_id, code)
        if isinstance(action, UncommitAction):
            code = self._refine_uncommit(record, code)
        return ApiError(code, str(rejection))

    def _refine_uncommit(self, record: MatchRecord, code: ApiErrorCode) -> ApiErrorCode:
        """Name the real reason an uncommit came too late.

        The engine's own all-committed check can never fire: unanimity resolves
        the play in the same transition, so a later caller meets a phase guard
        instead and is told whose turn it is. That answers a question they did
        not ask. What actually happened is that the play was committed and can
        no longer be withdrawn from, which is the rule §7.2 states.
        """
        withdrawable = code in (ApiErrorCode.WRONG_PHASE, ApiErrorCode.NOT_YOUR_TURN)
        committed_play = record.state.phase in (Phase.RESOLVING, Phase.AWAITING_ROW_CHOICE)
        if record.protocol.negotiation_enabled and withdrawable and committed_play:
            return ApiErrorCode.CANNOT_UNCOMMIT_WHEN_ALL_COMMITTED
        return code

    def _refine_row_choice(self, record: MatchRecord, player_id: str, code: ApiErrorCode) -> ApiErrorCode:
        """Tell a late row choice apart from one that was never pending.

        The engine cannot: closing a play clears its resolution state, so by the
        time a duplicate arrives nothing in the state remembers the choice. The
        log does, and both events involved are public.
        """
        required = [event for event in record.stream.events if event.type == EventType.ROW_CHOICE_REQUIRED]
        if not required:
            return code
        latest = required[-1]
        if latest.data.get("player_id") != player_id:
            return code
        answered = any(
            event.type == EventType.ROW_CHOICE_MADE and (event.hand, event.play) == (latest.hand, latest.play)
            for event in record.stream.events
        )
        return ApiErrorCode.ROW_ALREADY_CHOSEN if answered else code
