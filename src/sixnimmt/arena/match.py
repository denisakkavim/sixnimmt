"""Execute one match and publish accepted decisions."""

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import JsonValue
from typing_extensions import TypedDict

from sixnimmt.arena.bots.base import ActionBatch, Bot, Rejection, memory_bot, traced_bot
from sixnimmt.arena.bots.diagnostics import decision_evaluation
from sixnimmt.arena.bots.lifecycle import (
    BotContext,
    BotMatchEnd,
    ControllerStopped,
    DecisionOutcome,
    cancel_bot,
    close_bot,
    lifecycle_owners,
    settle_decision,
    start_bot,
)
from sixnimmt.arena.config import RunConfig, resolve, resolve_settings, resolved_abandoned_limit
from sixnimmt.arena.decisions import (
    AbandonedDecisions,
    Decision,
    DecisionMetrics,
    RoundRobinScheduler,
    Scheduler,
    SequentialScheduler,
    StopSignal,
    decide,
    prepare_batch,
)
from sixnimmt.arena.results import MatchOutcome, MatchResult
from sixnimmt.arena.results import public_reason as _safe_reason
from sixnimmt.arena.tracing import write_standalone_manifest
from sixnimmt.engine.actions import Action, SelectCardAction
from sixnimmt.engine.audience import Viewer
from sixnimmt.engine.errors import EngineRejection
from sixnimmt.engine.events import (
    ActionRejectedEvent,
    Event,
    MatchAbandonedEvent,
    MatchEndedEvent,
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
ActivityObserver = Callable[[dict[str, JsonValue | tuple[str, ...]]], None]


class DecisionActivityContext(TypedDict, closed=True):
    decision_number: int
    view_id: str
    hand_number: int
    play_number: int
    phase: str
    cards_remaining: int


@runtime_checkable
class ModelTraceSink(Protocol):
    def record_model(self, payload: dict[str, Any], *, player_id: str, display_name: str) -> None: ...


def _decision_actions(decision: Decision) -> tuple[Action, ...]:
    if isinstance(decision.action, ActionBatch):
        return decision.action.actions
    if decision.action is None:
        return ()
    return (decision.action,)


class ArenaError(RuntimeError):
    """A run cannot proceed because its harness or recording failed."""


class _BotLifecycle:
    def __init__(self) -> None:
        self.started: list[tuple[Bot, int]] = []
        self.owners: set[int] = set()
        self.errors: list[tuple[str, str, str]] = []

    def start(self, bot: Bot, seat: int, context: BotContext) -> None:
        owners = lifecycle_owners(bot)
        if len(owners & self.owners) > 0:
            msg = "a managed bot instance cannot be shared between seats"
            raise ValueError(msg)
        self.owners.update(owners)
        # Close even a partially initialized hook; close must be idempotent.
        self.started.append((bot, seat))
        start_bot(bot, context)

    def cancel(self, bot: Bot, player_id: str, reason: str) -> None:
        try:
            cancel_bot(bot, reason)
        except Exception as error:
            self.errors.append((player_id, "cancel", repr(error)))

    def close(self, match: "_Match", result: MatchResult | None) -> None:
        scores = MappingProxyType({player.player_id: player.total_score for player in match.state.players})
        for bot, seat in reversed(self.started):
            player_id = match.state.players[seat].player_id
            end = None
            if result is not None:
                end = BotMatchEnd(
                    result.outcome, result.winners, scores, _safe_reason(result.reason), match.folders[seat].view()
                )
            try:
                close_bot(bot, end)
            except Exception as error:
                self.errors.append((player_id, "close", repr(error)))
        self.started.clear()

    def close_unstarted(self, bots: Sequence[Bot], seats: Sequence[PlayerSeat]) -> None:
        """The job owns constructed resources until start transfers them here."""
        for bot, seat in reversed(tuple(zip(bots, seats, strict=False))):
            owners = lifecycle_owners(bot)
            if len(owners & self.owners) > 0:
                continue
            self.owners.update(owners)
            try:
                close_bot(bot, None)
            except Exception as error:
                self.errors.append((seat.player_id, "close", repr(error)))


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
        on_activity: ActivityObserver | None = None,
        metrics: DecisionMetrics | None = None,
        lifecycle: _BotLifecycle | None = None,
    ) -> None:
        self.seed = seed
        self.rules = rules
        self.protocol = protocol
        self.config = resolve_settings(config, protocol).match
        self.sink = sink
        self.observer = observer
        self.on_activity = on_activity
        self.metrics = metrics
        self.state, initial = create_match(match_id, seats, seed, rules=rules, protocol=protocol)
        self.events: list[Event] = []
        self.folders = [ViewFolder(Viewer(ViewRole.PLAYER, seat.player_id)) for seat in seats]
        self.accepted = [0] * len(seats)
        self.rejected = [0] * len(seats)
        self.decision_numbers = [0] * len(seats)
        self.action_seq = 0
        self.play_attempts = 0
        self.lifecycle = _BotLifecycle() if lifecycle is None else lifecycle
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
            final_event = self.events[-1]
            if not isinstance(final_event, MatchEndedEvent):
                msg = "finished match requires a match_ended event"
                raise ArenaError(msg)
            winners = tuple(final_event.data["winners"])
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

    def offer(
        self, bot: Bot, seat: int, abandoned: AbandonedDecisions, stop_event: StopSignal | None = None
    ) -> MatchResult | None:
        rejection = None
        rejections = 0
        player_id = self.state.players[seat].player_id
        while True:
            view = self.folders[seat].view()
            self.decision_numbers[seat] += 1
            context: DecisionActivityContext = {
                "decision_number": self.decision_numbers[seat],
                "view_id": view.view_id,
                "hand_number": view.hand_number,
                "play_number": view.play_number,
                "phase": view.phase.value,
                "cards_remaining": len(view.you.hand),
            }
            started = datetime.now(UTC)
            timeout = self.config.decision_timeout_seconds
            deadline = None if timeout is None else (started + timedelta(seconds=timeout)).isoformat()
            self.activity(seat, "decision_started", **context, deadline=deadline)
            decision = decide(bot, view, rejection, self.config.decision_timeout_seconds, abandoned, stop_event)
            if decision.timed_out:
                self.lifecycle.cancel(bot, player_id, "decision_timeout")
            elif isinstance(decision.error, ControllerStopped):
                self.lifecycle.cancel(bot, player_id, "operator_stop")
            result, rejection, outcome = self.publish_decision(bot, seat, decision)
            self.report_completion(seat, decision, outcome, result, rejection, context)
            if outcome.status == "accepted" and decision.error is None and not decision.timed_out:
                self.report_evaluation(bot, seat, decision, context)
            if result is not None or rejection is None:
                return result
            rejections += 1
            terminal = self.check_limits([], rejections, player_id, rejection)
            if terminal is not None:
                return terminal
            rejection = replace(rejection, legal_actions=self.folders[seat].view().legal_actions)

    def activity(self, seat: int, kind: str, **data: JsonValue) -> None:
        if self.on_activity is None:
            return
        player = self.state.players[seat]
        self.on_activity({
            "type": kind,
            "player_id": player.player_id,
            "display_name": player.display_name,
            "timestamp": datetime.now(UTC).isoformat(),
            **data,
        })

    def report_completion(
        self,
        seat: int,
        decision: Decision,
        outcome: DecisionOutcome,
        result: MatchResult | None,
        rejection: Rejection | None,
        context: DecisionActivityContext,
    ) -> None:
        if self.on_activity is None:
            return
        reason = decision_reason(decision, rejection)
        if reason is None and outcome.status == "failed":
            reason = result.reason if result is not None else outcome.reason
        actions = [
            action.model_dump(mode="json", exclude={"action_id", "from_view", "expected_view_version"})
            for action in _decision_actions(decision)
        ]
        accepted = outcome.status == "accepted"
        self.activity(
            seat,
            "decision_finished",
            **context,
            status=outcome.status,
            text=reason,
            actions=actions if accepted else [],
            attempted_actions=[] if accepted else actions,
        )

    def report_evaluation(self, bot: Bot, seat: int, decision: Decision, context: DecisionActivityContext) -> None:
        if self.on_activity is None:
            return
        chosen_card = next(
            (action.card for action in reversed(_decision_actions(decision)) if isinstance(action, SelectCardAction)),
            None,
        )
        if chosen_card is None:
            return
        try:
            evaluation = decision_evaluation(bot, context["cards_remaining"])
        except Exception:
            # Diagnostics cannot invalidate an already published move.
            return
        if evaluation is not None:
            self.activity(seat, "simulation_evaluation", **context, chosen_card=chosen_card, **evaluation.as_activity())

    def publish_decision(
        self, bot: Bot, seat: int, decision: Decision
    ) -> tuple[MatchResult | None, Rejection | None, DecisionOutcome]:
        accepted_before = self.accepted[seat]
        player_id = self.state.players[seat].player_id
        try:
            if self.metrics is not None:
                self.metrics.record_call(player_id, decision.duration_ms)
            if isinstance(decision.action, ActionBatch):
                result, rejection = self.apply_batch(bot, seat, decision, decision.action)
            else:
                result, rejection = self.apply_single(seat, decision)
        except BaseException:
            self.notify_decision(
                bot, player_id, DecisionOutcome("failed", reason="publication_failed"), propagate=False
            )
            raise
        if rejection is not None:
            rejection = replace(rejection, legal_actions=self.folders[seat].view().legal_actions)
            outcome = DecisionOutcome("rejected", rejection=rejection)
        elif self.accepted[seat] > accepted_before:
            outcome = DecisionOutcome("accepted")
        else:
            reason = result.reason if result is not None else decision_reason(decision, None)
            outcome = DecisionOutcome("failed", reason=_safe_reason(reason))
        self.notify_decision(bot, player_id, outcome)
        return result, rejection, outcome

    def notify_decision(self, bot: Bot, player_id: str, outcome: DecisionOutcome, *, propagate: bool = True) -> None:
        try:
            settle_decision(bot, outcome)
        except Exception as error:
            # A notification cannot roll back already published game actions.
            self.lifecycle.errors.append((player_id, "settle_decision", repr(error)))
            if propagate:
                msg = "bot decision settlement failed"
                raise ArenaError(msg) from error

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
            if isinstance(decision.error, ControllerStopped):
                return self.finish(MatchOutcome.ABANDONED, player_id, "operator_stop"), None
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
    if isinstance(decision.error, ControllerStopped):
        return "operator_stop"
    if decision.error is not None:
        return repr(decision.error)
    if refused is not None:
        return f"{refused.code.value}: {refused.message}"
    return None


def _record_bot_trace(
    payload: dict[str, Any],
    *,
    model_sink: ModelTraceSink | None,
    on_activity: ActivityObserver | None,
    player_id: str,
    display_name: str,
) -> None:
    if model_sink is not None:
        model_sink.record_model(payload, player_id=player_id, display_name=display_name)
    if on_activity is not None:
        on_activity({**payload, "player_id": player_id, "display_name": display_name})


def _attach_bot_traces(
    bots: Sequence[Bot], seats: Sequence[PlayerSeat], sink: EventSink, on_activity: ActivityObserver | None = None
) -> None:
    model_sink = sink if isinstance(sink, ModelTraceSink) else None
    for bot, player in zip(bots, seats, strict=True):
        traced = traced_bot(bot)
        if traced is None:
            continue
        callback = None
        if model_sink is not None or on_activity is not None:
            callback = partial(
                _record_bot_trace,
                model_sink=model_sink,
                on_activity=on_activity,
                player_id=player.player_id,
                display_name=player.name_or_id,
            )
        traced.set_trace(callback)


def _run_bots(
    match: _Match,
    bots: Sequence[Bot],
    scheduler: Scheduler,
    abandoned: AbandonedDecisions,
    stop_event: StopSignal | None,
) -> MatchResult:
    for seat, bot in enumerate(bots):
        player_id = match.state.players[seat].player_id
        context = BotContext(match.state.match_id, player_id, match.rules, match.protocol)
        try:
            match.lifecycle.start(bot, seat, context)
        except ControllerStopped:
            return match.finish(MatchOutcome.ABANDONED, reason="operator_stop")
        except Exception as error:
            match.lifecycle.errors.append((player_id, "start", repr(error)))
            return match.finish(MatchOutcome.FAILED, player_id, repr(error))
    while True:
        if stop_event is not None and stop_event.is_set():
            return match.finish(MatchOutcome.ABANDONED, reason="operator_stop")
        seat = match.acting_seat(scheduler)
        result = match.offer(bots[seat], seat, abandoned, stop_event)
        if result is not None:
            return result


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
    on_activity: ActivityObserver | None = None,
    max_actions: int | None = None,
    sink: EventSink | None = None,
    stop_event: StopSignal | None = None,
    _abandoned: AbandonedDecisions | None = None,
    _metrics: DecisionMetrics | None = None,
    _lifecycle: _BotLifecycle | None = None,
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
    lifecycle = _BotLifecycle() if _lifecycle is None else _lifecycle
    owned = sink is None
    if sink is None:
        sink = NullEventSink()
    try:
        if owned and config.trace_dir is not None:
            config.trace_dir.mkdir(parents=True, exist_ok=False)
            sink = JsonlEventSink(config.trace_dir, match_id)
        abandoned = AbandonedDecisions(resolved_abandoned_limit(config)) if _abandoned is None else _abandoned
        scheduler = RoundRobinScheduler() if config.scheduler == "round_robin" else SequentialScheduler()
        match = _Match(seed, match_id, seats, rules, protocol, config, sink, observer, on_activity, _metrics, lifecycle)
        result = None
        try:
            _attach_bot_traces(bots, seats, sink, on_activity)
            result = _run_bots(match, bots, scheduler, abandoned, stop_event)
        finally:
            if result is None:
                for bot, seat in match.lifecycle.started:
                    match.lifecycle.cancel(bot, seats[seat].player_id, "match_failed")
            match.lifecycle.close(match, result)
            lifecycle.close_unstarted(bots, seats)
        result = replace(result, lifecycle_errors=tuple(match.lifecycle.errors))
        if on_activity is not None:
            on_activity({
                "type": "match_finished",
                "outcome": result.outcome.value,
                "reason": result.reason,
                "winners": result.winners,
                "timestamp": datetime.now(UTC).isoformat(),
            })
        if owned and config.trace_dir is not None:
            write_standalone_manifest(result, bots, seats, rules, protocol, config, abandoned.total)
        return result
    finally:
        lifecycle.close_unstarted(bots, seats)
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
    on_activity: ActivityObserver | None = None,
    max_actions: int | None = None,
    sink: EventSink | None = None,
    stop_event: StopSignal | None = None,
) -> MatchResult:
    """Run one match. A supplied sink remains owned by the caller.

    Without a supplied sink, own and close any trace handles and write the
    standalone manifest. Observer callbacks receive privileged state.

    Optional bot lifecycle hooks run within this match's ownership. Setting
    stop_event abandons the match and asks the active bot to cancel its work.
    A blocked Python decision is detached; its late result is never applied.
    on_activity receives privileged diagnostics and must be fast and thread-safe.
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
        on_activity=on_activity,
        max_actions=max_actions,
        sink=sink,
        stop_event=stop_event,
    )
