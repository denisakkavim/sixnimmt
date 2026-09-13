"""Execute one match and publish accepted decisions."""

from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import partial
from typing import Literal

from sixnimmt.arena.bots.base import ActionBatch, Bot, Rejection, memory_bot, traced_bot
from sixnimmt.arena.config import RunConfig, resolve, resolved_abandoned_limit
from sixnimmt.arena.decisions import AbandonedDecisions, Decision, decide
from sixnimmt.arena.results import MatchOutcome, MatchResult
from sixnimmt.arena.scheduling import RoundRobinScheduler, Scheduler, SequentialScheduler
from sixnimmt.arena.tracing import write_standalone_manifest
from sixnimmt.arena.transactions import prepare_batch
from sixnimmt.engine.audience import Viewer
from sixnimmt.engine.errors import EngineRejection
from sixnimmt.engine.events import (
    ActionRejectedEvent,
    Event,
    MatchAbandonedEvent,
    assign_sequence,
    audience_for_player,
)
from sixnimmt.engine.fold import ViewFolder
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.state import MatchState, Phase, PlayerSeat
from sixnimmt.engine.transition import transition
from sixnimmt.engine.views import ViewRole
from sixnimmt.persistence.sink import ActionRecord, EventSink, JsonlEventSink, NullEventSink

ActionOutcome = Literal["accepted", "rejected", "timeout", "error"]
Observer = Callable[[MatchState, tuple[Event, ...]], None]


class ArenaError(RuntimeError):
    """A run cannot proceed because its harness or recording failed."""


def _validate_player_count(count: int) -> None:
    if not 2 <= count <= 10:
        msg = "an arena match requires between 2 and 10 players"
        raise ValueError(msg)


class _Match:
    def __init__(
        self,
        seed: int,
        match_id: str,
        seats: Sequence[PlayerSeat],
        rules: GameRules,
        protocol: MatchProtocol,
        config: RunConfig,
        sink: EventSink,
        observer: Observer | None,
    ) -> None:
        self.seed = seed
        self.rules = rules
        self.protocol = protocol
        self.config = config
        self.sink = sink
        self.observer = observer
        self.state, initial = create_match(match_id, seats, seed, rules=rules, protocol=protocol)
        self.events: list[Event] = []
        self.folders = [ViewFolder(Viewer(ViewRole.PLAYER, seat.player_id)) for seat in seats]
        self.accepted = [0] * len(seats)
        self.rejected = [0] * len(seats)
        self.action_seq = 0
        self.play_attempts = 0
        self.append(initial)

    def append(self, events: Sequence[Event]) -> None:
        numbered = assign_sequence(list(events), len(self.events) + 1, self.action_seq)
        self.sink.append(numbered)
        self.events.extend(numbered)
        for folder in self.folders:
            folder.apply(numbered)
        if self.observer is not None:
            self.observer(self.state, tuple(numbered))

    def finish(self, outcome: MatchOutcome, ended_by: str | None = None, reason: str | None = None) -> MatchResult:
        winners: tuple[str, ...] = ()
        if outcome == MatchOutcome.FINISHED:
            winners = tuple(self.events[-1].data["winners"])
        else:
            self.append([
                # Exception details may contain private observations. Keep them
                # in the admin log, while announcing termination publicly.
                MatchAbandonedEvent(
                    match_id=self.state.match_id,
                    hand=self.state.hand_number,
                    play=self.state.play_number,
                    audience="admin",
                    data={"outcome": outcome.value, "ended_by": ended_by, "reason": reason},
                ),
                MatchAbandonedEvent(
                    match_id=self.state.match_id,
                    hand=self.state.hand_number,
                    play=self.state.play_number,
                    audience="public",
                ),
            ])
        return MatchResult(
            self.seed,
            outcome,
            self.state,
            tuple(self.events),
            winners,
            sum(self.accepted),
            sum(self.rejected),
            ended_by,
            reason,
            tuple(zip(self.accepted, self.rejected, strict=True)),
        )

    def acting_seat(self, scheduler: Scheduler) -> int:
        if self.state.phase == Phase.AWAITING_ROW_CHOICE and self.state.resolution is not None:
            return next(
                index
                for index, player in enumerate(self.state.players)
                if player.player_id == self.state.resolution.awaiting_player
            )
        if self.state.phase == Phase.SELECTING:
            seat = scheduler.next_seat(self.state)
            if seat is not None:
                return seat
        msg = f"no bot decision available during {self.state.phase.value}"
        raise ArenaError(msg)

    def offer(self, bot: Bot, seat: int, abandoned: AbandonedDecisions) -> MatchResult | None:
        rejection = None
        rejections = 0
        player_id = self.state.players[seat].player_id
        while True:
            view = self.folders[seat].view()
            decision = decide(bot, view, rejection, self.config.decision_timeout_seconds, abandoned)
            if isinstance(decision.action, ActionBatch):
                result, rejection = self.apply_batch(bot, seat, decision, decision.action)
            else:
                result, rejection = self.apply_single(seat, decision)
            if result is not None or rejection is None:
                return result
            rejections += 1
            terminal = self.check_limits([], rejections, player_id, rejection)
            if terminal is not None:
                return terminal
            rejection = replace(rejection, legal_actions=self.folders[seat].view().legal_actions)

    def record_decision(
        self,
        seat: int,
        decision: Decision,
        action_type: str | None,
        outcome: ActionOutcome,
        reason: str | None,
        from_view: str | None = None,
        record_timing: bool = True,
    ) -> None:
        self.action_seq += 1
        self.sink.record_action(
            ActionRecord(
                server_action_seq=self.action_seq,
                action_id=f"{self.state.match_id}:{self.action_seq}",
                player_id=self.state.players[seat].player_id,
                type=action_type,
                from_view=from_view if from_view is not None else self.folders[seat].view().view_id,
                received_at=decision.ended_at,
                outcome=outcome,
                reason=reason,
                decision_started_at=decision.started_at,
                decision_ended_at=decision.ended_at,
                decision_duration_ms=decision.duration_ms if record_timing else None,
            )
        )

    def apply_single(self, seat: int, decision: Decision) -> tuple[MatchResult | None, Rejection | None]:
        action = decision.action
        if isinstance(action, ActionBatch):
            msg = "batch must be preflighted separately"
            raise ArenaError(msg)
        outcome, next_state, events, refused = self.attempt(seat, decision)
        reason = decision_reason(decision, refused)
        self.record_decision(seat, decision, action.type.value if action is not None else None, outcome, reason)
        player_id = self.state.players[seat].player_id
        if outcome in ("timeout", "error"):
            return self.finish(MatchOutcome.FAILED, player_id, reason), None
        self.play_attempts += 1
        if outcome == "accepted":
            self.accepted[seat] += 1
            self.state = next_state
        else:
            self.rejected[seat] += 1
        self.append(events)
        if refused is not None:
            return None, refused
        return self.check_limits(events, 0, player_id, None), None

    def apply_batch(
        self, bot: Bot, seat: int, decision: Decision, proposal: ActionBatch
    ) -> tuple[MatchResult | None, Rejection | None]:
        if self.action_seq + proposal.size > self.config.match_action_limit:
            return self.finish(MatchOutcome.ABANDONED, reason="match_action_limit"), None
        if (
            self.config.play_action_limit is not None
            and self.play_attempts + proposal.size > self.config.play_action_limit
        ):
            return self.finish(MatchOutcome.ABANDONED, reason="play_action_limit"), None
        player_id = self.state.players[seat].player_id
        prepared = prepare_batch(self.state, player_id, proposal, self.protocol, self.rules)
        if prepared.rejection is not None:
            self.reject_batch(seat, decision, proposal, prepared.rejection)
            return None, prepared.rejection
        # No game effects or notebook writes escape preflight. Once validated,
        # publish each action with its own sequence number and apply memory once.
        if proposal.memory is not None:
            memory = memory_bot(bot)
            if memory is None:
                msg = "bot does not support transactional memory"
                raise ArenaError(msg)
            try:
                memory.accept_batch(proposal)
            except Exception as error:
                reason = repr(error)
                self.record_decision(seat, decision, "update_memory", "error", reason)
                return self.finish(MatchOutcome.FAILED, player_id, reason), None
        all_events = []
        offered_view = self.folders[seat].view().view_id
        for index, (action, (state, events)) in enumerate(zip(proposal.actions, prepared.steps, strict=True)):
            self.record_decision(
                seat, decision, action.type.value, "accepted", None, from_view=offered_view, record_timing=index == 0
            )
            self.state = state
            self.append(events)
            all_events.extend(events)
        if proposal.memory is not None:
            self.record_decision(
                seat,
                decision,
                "update_memory",
                "accepted",
                None,
                from_view=offered_view,
                record_timing=len(proposal.actions) == 0,
            )
        self.play_attempts += proposal.size
        self.accepted[seat] += proposal.size
        return self.check_limits(all_events, 0, player_id, None), None

    def reject_batch(self, seat: int, decision: Decision, proposal: ActionBatch, rejection: Rejection) -> None:
        names = [action.type.value for action in proposal.actions]
        if proposal.memory is not None:
            names.append("update_memory")
        for index, name in enumerate(names):
            self.record_decision(seat, decision, name, "rejected", rejection.message, record_timing=index == 0)
        self.play_attempts += proposal.size
        self.rejected[seat] += proposal.size
        self.append([
            ActionRejectedEvent(
                match_id=self.state.match_id,
                hand=self.state.hand_number,
                play=self.state.play_number,
                audience=audience_for_player(self.state.players[seat].player_id),
                data={
                    "code": rejection.code.value,
                    "message": rejection.message,
                    "action_type": rejection.action.type.value if rejection.action is not None else "transaction",
                },
            )
        ])

    def attempt(self, seat: int, decision: Decision) -> tuple[ActionOutcome, MatchState, list[Event], Rejection | None]:
        if decision.timed_out or decision.error is not None:
            outcome = "timeout" if decision.timed_out else "error"
            return outcome, self.state, [], None
        action = decision.action
        if isinstance(action, ActionBatch):
            msg = "batch must be preflighted separately"
            raise ArenaError(msg)
        if action is None:
            msg = "successful decision did not carry an action"
            raise ArenaError(msg)
        player_id = self.state.players[seat].player_id
        try:
            state, events = transition(self.state, player_id, action, self.protocol, self.rules)
        except EngineRejection as error:
            rejection = Rejection(error.code, str(error), (), action)
            event = ActionRejectedEvent(
                match_id=self.state.match_id,
                hand=self.state.hand_number,
                play=self.state.play_number,
                audience=audience_for_player(player_id),
                data={"code": error.code.value, "message": str(error), "action_type": action.type.value},
            )
            return "rejected", self.state, [event], rejection
        return "accepted", state, events, None

    def check_limits(
        self, batch: Sequence[Event], rejections: int, player_id: str, rejection: Rejection | None
    ) -> MatchResult | None:
        if any(event.type == "match_ended" for event in batch):
            return self.finish(MatchOutcome.FINISHED)
        if any(event.type == "play_started" for event in batch):
            self.play_attempts = 0
        if rejections >= self.config.decision_rejection_limit and rejection is not None:
            return self.finish(MatchOutcome.FORFEITED, player_id, rejection.code.value)
        if self.config.play_action_limit is not None and self.play_attempts >= self.config.play_action_limit:
            return self.finish(MatchOutcome.ABANDONED, reason="play_action_limit")
        if self.action_seq >= self.config.match_action_limit:
            return self.finish(MatchOutcome.ABANDONED, reason="match_action_limit")
        return None


def decision_reason(decision: Decision, refused: Rejection | None) -> str | None:
    if decision.timed_out:
        return "decision_timeout"
    if decision.error is not None:
        return repr(decision.error)
    if refused is not None:
        return f"{refused.code.value}: {refused.message}"
    return None


def _attach_bot_traces(bots: Sequence[Bot], seats: Sequence[PlayerSeat], sink: EventSink) -> None:
    record_model = getattr(sink, "record_model", None)
    for bot, player in zip(bots, seats, strict=True):
        traced = traced_bot(bot)
        if traced is None:
            continue
        callback = None
        if record_model is not None:
            callback = partial(record_model, player_id=player.player_id, display_name=player.name_or_id)
        traced.set_trace(callback)


def _run_match(
    bots: Sequence[Bot],
    seed: int,
    *,
    match_id: str = "arena_0",
    rules: GameRules | None = None,
    protocol: MatchProtocol | None = None,
    config: RunConfig | None = None,
    seats: Sequence[PlayerSeat] | None = None,
    observer: Observer | None = None,
    max_actions: int | None = None,
    sink: EventSink | None = None,
    _abandoned: AbandonedDecisions | None = None,
) -> MatchResult:
    """Run trusted bots on folded views. Observer is privileged and must be thread-safe.

    A supplied sink belongs to the caller. Without one, trace_dir is created
    exclusively and this function owns and closes the match's trace handles.
    """
    _validate_player_count(len(bots))
    rules = GameRules() if rules is None else rules
    protocol = MatchProtocol() if protocol is None else protocol
    config = RunConfig() if config is None else config
    if max_actions is not None:
        config = replace(config, match_action_limit=max_actions)
    config = resolve(config, protocol)
    seats = seats if seats is not None else [PlayerSeat(player_id=f"player_{i + 1}") for i in range(len(bots))]
    if len(seats) != len(bots):
        msg = "seats and bots must have the same length"
        raise ValueError(msg)
    owned = sink is None
    if sink is None:
        sink = NullEventSink()
        if config.trace_dir is not None:
            config.trace_dir.mkdir(parents=True, exist_ok=False)
            sink = JsonlEventSink(config.trace_dir, match_id)
    abandoned = AbandonedDecisions(resolved_abandoned_limit(config)) if _abandoned is None else _abandoned
    scheduler = RoundRobinScheduler() if config.scheduler == "round_robin" else SequentialScheduler()
    try:
        match = _Match(seed, match_id, seats, rules, protocol, config, sink, observer)
        _attach_bot_traces(bots, seats, sink)
        while True:
            seat = match.acting_seat(scheduler)
            result = match.offer(bots[seat], seat, abandoned)
            if result is not None:
                if owned and config.trace_dir is not None:
                    write_standalone_manifest(result, bots, seats, rules, protocol, config, abandoned.total)
                return result
    finally:
        if owned:
            sink.close()


def run_match(
    bots: Sequence[Bot],
    seed: int,
    *,
    match_id: str = "arena_0",
    rules: GameRules | None = None,
    protocol: MatchProtocol | None = None,
    config: RunConfig | None = None,
    seats: Sequence[PlayerSeat] | None = None,
    observer: Observer | None = None,
    max_actions: int | None = None,
    sink: EventSink | None = None,
) -> MatchResult:
    """Run one match. A supplied sink remains owned by the caller.

    Without a supplied sink, own and close any trace handles and write the
    standalone manifest. Observer callbacks receive privileged state.
    """
    return _run_match(
        bots,
        seed,
        match_id=match_id,
        rules=rules,
        protocol=protocol,
        config=config,
        seats=seats,
        observer=observer,
        max_actions=max_actions,
        sink=sink,
    )
