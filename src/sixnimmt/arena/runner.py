"""Sequential matches and bounded concurrent runs over the pure rules engine."""

import hashlib
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, replace
from datetime import UTC, datetime
from functools import partial
from typing import Literal

from sixnimmt.arena.bots import Bot, Rejection
from sixnimmt.arena.bots.base import ActionBatch
from sixnimmt.arena.config import RunConfig as RunConfig
from sixnimmt.arena.config import resolve as resolve
from sixnimmt.arena.decisions import AbandonedDecisions, Decision, decide
from sixnimmt.arena.players import PlayerConfig as PlayerConfig
from sixnimmt.arena.players import ResolvedPlayer, resolve_players
from sixnimmt.arena.results import ArenaResult as ArenaResult
from sixnimmt.arena.results import MatchOutcome as MatchOutcome
from sixnimmt.arena.results import MatchResult as MatchResult
from sixnimmt.arena.results import SeatResult
from sixnimmt.arena.scheduling import RoundRobinScheduler, Scheduler, SequentialScheduler
from sixnimmt.arena.tracing import collect_stats, write_standalone_manifest
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
from sixnimmt.persistence.manifest import ManifestMatch, write_manifest
from sixnimmt.persistence.sink import ActionRecord, EventSink, JsonlEventSink, NullEventSink

ActionOutcome = Literal["accepted", "rejected", "timeout", "error"]

Observer = Callable[[MatchState, tuple[Event, ...]], None]
DEFAULT_MAX_ACTIONS = 10_000


class ArenaError(RuntimeError):
    """A run cannot proceed because its harness or recording failed."""


def derive_seed(seed: int, domain: str, game_index: int, seat_index: int | None = None) -> int:
    """Stable independent streams, derived without consuming another RNG."""
    value = f"sixnimmt-arena:{seed}:{domain}:{game_index}:{seat_index}"
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")


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
            accept = getattr(bot, "accept_batch", None)
            if accept is None:
                msg = "bot does not support transactional memory"
                raise ArenaError(msg)
            accept(proposal)
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
                record_timing=not proposal.actions,
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
        set_trace = getattr(bot, "set_trace", None)
        if set_trace is None:
            continue
        callback = None
        if record_model is not None:
            callback = partial(record_model, player_id=player.player_id, display_name=player.name_or_id)
        set_trace(callback)


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
    _abandoned: AbandonedDecisions | None = None,
) -> MatchResult:
    """Run trusted bots on folded views. Observer is privileged and must be thread-safe.

    A supplied sink belongs to the caller. Without one, trace_dir is created
    exclusively and this function owns and closes the match's trace handles.
    """
    _validate_player_count(len(bots))
    rules = rules or GameRules()
    protocol = protocol or MatchProtocol()
    config = config or RunConfig()
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
    abandoned = _abandoned or AbandonedDecisions(config.max_abandoned_decisions or 0)
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


def _play_game(
    index: int,
    seed: int,
    specs: Sequence[ResolvedPlayer],
    seats: Sequence[PlayerSeat],
    rules: GameRules,
    protocol: MatchProtocol,
    config: RunConfig,
    run_id: str,
    abandoned: AbandonedDecisions,
    observer: Observer | None,
    initial_bots: Sequence[Bot] | None = None,
) -> tuple[MatchResult, ManifestMatch]:
    match_seed = derive_seed(seed, "match", index)
    match_id = f"arena_{index}"
    filename = f"{run_id}_{index}"
    sink = JsonlEventSink(config.trace_dir, filename) if config.trace_dir is not None else NullEventSink()
    bots: list[Bot] = list(initial_bots) if initial_bots is not None else []
    try:
        build_error = None
        if initial_bots is None:
            for seat, spec in enumerate(specs):
                try:
                    bots.append(spec.build(derive_seed(seed, "bot", index, seat)))
                except Exception as error:
                    build_error = error
                    break
        if build_error is not None:
            match = _Match(match_seed, match_id, seats, rules, protocol, config, sink, observer)
            result = match.finish(MatchOutcome.FAILED, f"player_{len(bots) + 1}", repr(build_error))
        else:
            result = run_match(
                bots,
                match_seed,
                match_id=match_id,
                rules=rules,
                protocol=protocol,
                config=config,
                seats=seats,
                observer=observer,
                sink=sink,
                _abandoned=abandoned,
            )
        stats, errors = collect_stats(bots, result.ended_by if result.reason == "decision_timeout" else None)
        for seat in seats:
            stats.setdefault(seat.player_id, None)
        entry = ManifestMatch(
            game_index=index,
            match_id=match_id,
            seed=match_seed,
            outcome=result.outcome.value,
            winners=result.winners,
            ended_by=result.ended_by,
            reason=result.reason,
            log=f"{filename}.jsonl",
            actions=f"{filename}.actions.jsonl",
            seat_stats=stats,
            stats_errors=errors,
        )
        return result, entry
    finally:
        sink.close()


class _Aggregate:
    def __init__(self, players: Sequence[str], display_names: Sequence[str]) -> None:
        self.players = players
        self.display_names = display_names
        self.counts = dict.fromkeys(MatchOutcome, 0)
        self.hands = 0
        self.actions = 0
        self.wins = [0] * len(players)
        self.ties = [0] * len(players)
        self.scores = [0] * len(players)
        self.accepted = [0] * len(players)
        self.rejected = [0] * len(players)

    def add(self, result: MatchResult) -> None:
        self.counts[result.outcome] += 1
        self.hands += result.final_state.hand_number
        self.actions += result.actions
        for index, player in enumerate(result.final_state.players):
            accepted, rejected = result.seat_actions[index]
            self.accepted[index] += accepted
            self.rejected[index] += rejected
            if result.outcome != MatchOutcome.FINISHED:
                continue
            self.scores[index] += player.total_score
            if player.player_id in result.winners:
                if len(result.winners) == 1:
                    self.wins[index] += 1
                else:
                    self.ties[index] += 1

    def result(
        self, run_id: str, seed: int, games: int, started: int, abandoned: int, reproducible: bool
    ) -> ArenaResult:
        seats = tuple(
            SeatResult(
                f"player_{i + 1}",
                name,
                self.wins[i],
                self.ties[i],
                self.scores[i],
                self.accepted[i],
                self.rejected[i],
                self.display_names[i],
            )
            for i, name in enumerate(self.players)
        )
        return ArenaResult(
            run_id,
            seed,
            games,
            started,
            sum(self.counts.values()),
            self.counts[MatchOutcome.FINISHED],
            self.counts[MatchOutcome.ABANDONED],
            self.counts[MatchOutcome.FORFEITED],
            self.counts[MatchOutcome.FAILED],
            abandoned,
            self.hands,
            self.actions,
            reproducible,
            seats,
        )


def _drive_games(
    games: int,
    seed: int,
    specs: Sequence[ResolvedPlayer],
    seats: Sequence[PlayerSeat],
    rules: GameRules,
    protocol: MatchProtocol,
    config: RunConfig,
    run_id: str,
    abandoned: AbandonedDecisions,
    observer: Observer | None,
    initial_bots: Sequence[Bot],
    aggregate: _Aggregate,
    entries: list[ManifestMatch],
) -> tuple[int, BaseException | None]:
    started = 0
    fatal: BaseException | None = None
    stop = False
    with ThreadPoolExecutor(max_workers=config.concurrency, thread_name_prefix="arena-match") as pool:
        pending: dict[Future[tuple[MatchResult, ManifestMatch]], int] = {}
        while pending or (started < games and not stop):
            while (
                started < games and len(pending) < config.concurrency and not stop and not abandoned.exceeded.is_set()
            ):
                future = pool.submit(
                    _play_game,
                    started,
                    seed,
                    specs,
                    seats,
                    rules,
                    protocol,
                    config,
                    run_id,
                    abandoned,
                    observer,
                    initial_bots if started == 0 else None,
                )
                pending[future] = started
                started += 1
            if not pending:
                break
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                pending.pop(future)
                try:
                    result, entry = future.result()
                except Exception as error:
                    fatal = fatal or error
                    stop = True
                    continue
                aggregate.add(result)
                if config.trace_dir is not None:
                    entries.append(entry)
                if config.stop_on_failure and result.outcome == MatchOutcome.FAILED:
                    stop = True
            if abandoned.exceeded.is_set():
                stop = True
                fatal = fatal or ArenaError("max_abandoned_decisions exceeded; stopped submitting matches")
    return started, fatal


def run_arena(
    players: Sequence[PlayerConfig],
    games: int,
    seed: int,
    *,
    rules: GameRules | None = None,
    protocol: MatchProtocol | None = None,
    config: RunConfig | None = None,
    max_actions_per_match: int | None = None,
    observer: Observer | None = None,
) -> ArenaResult:
    """Run fresh seats per match, retaining full provenance only when tracing."""
    _validate_player_count(len(players))
    if games < 1:
        msg = "games must be positive"
        raise ValueError(msg)
    rules = rules or GameRules()
    protocol = protocol or MatchProtocol()
    config = config or RunConfig()
    if max_actions_per_match is not None:
        config = replace(config, match_action_limit=max_actions_per_match)
    config = resolve(config, protocol)
    specs = resolve_players(players)
    if not rules.min_players <= len(players) <= rules.max_players:
        msg = "player count is outside the configured game rules"
        raise ValueError(msg)
    seats = [
        PlayerSeat(
            player_id=f"player_{i + 1}",
            display_name=spec.config.display_name if spec.config.display_name is not None else f"Player {i + 1}",
            agent_metadata=spec.metadata,
        )
        for i, spec in enumerate(specs)
    ]
    # Construct game zero before any submission; later construction failures are
    # transient match outcomes, while a broken initial lineup is a run error.
    try:
        initial_bots = [spec.build(derive_seed(seed, "bot", 0, i)) for i, spec in enumerate(specs)]
    except Exception as error:
        msg = f"cannot construct first game's lineup: {error!r}"
        raise ArenaError(msg) from error
    run_id = f"arena_{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{seed}"
    if config.trace_dir is not None:
        config.trace_dir.mkdir(parents=True, exist_ok=False)
    abandoned = AbandonedDecisions(config.max_abandoned_decisions or 0)
    aggregate = _Aggregate([spec.name for spec in specs], [seat.display_name for seat in seats])
    entries: list[ManifestMatch] = []
    started, fatal = _drive_games(
        games,
        seed,
        specs,
        seats,
        rules,
        protocol,
        config,
        run_id,
        abandoned,
        observer,
        initial_bots,
        aggregate,
        entries,
    )
    result = aggregate.result(run_id, seed, games, started, abandoned.total, all(spec.deterministic for spec in specs))
    if config.trace_dir is not None:
        manifest = {
            "manifest_version": 1,
            "run_id": run_id,
            "seed": seed,
            "games_requested": games,
            "games_started": started,
            "games_completed": result.games_completed,
            "reproducible": result.reproducible,
            "rules": rules.model_dump(mode="json"),
            "protocol": protocol.model_dump(mode="json"),
            "run_config": {key: value for key, value in asdict(config).items() if key != "trace_dir"},
            "seats": [
                {
                    "player_id": seat.player_id,
                    "bot": spec.name,
                    "display_name": seat.display_name,
                    "options": spec.recorded_options,
                    "deterministic": spec.deterministic,
                    "agent_metadata": seat.agent_metadata,
                }
                for seat, spec in zip(seats, specs, strict=True)
            ],
            "matches": [entry.model_dump(mode="json") for entry in sorted(entries, key=lambda entry: entry.game_index)],
            "decisions_abandoned": abandoned.total,
            "error": repr(fatal) if fatal is not None else None,
        }
        write_manifest(config.trace_dir, manifest)
    if fatal is not None:
        msg = f"arena run failed after {result.games_completed}/{started} started matches completed: {fatal}"
        raise ArenaError(msg) from fatal
    return result
