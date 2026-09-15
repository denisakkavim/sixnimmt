"""Construct and supervise one table of native, managed, and registered bots."""

import json
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from threading import Event, Thread

from pydantic import Field, JsonValue, model_validator

from sixnimmt.arena.bots.base import Bot
from sixnimmt.arena.bots.external import CommandOptions, HarnessBot, HeadlessOptions, ManagedHarnessBot
from sixnimmt.arena.bots.external_harnesses.broker import SeatSession
from sixnimmt.arena.bots.external_harnesses.connection import write_connection_bundle
from sixnimmt.arena.bots.external_harnesses.drivers import CommandProfile, ManagedCommandDriver
from sixnimmt.arena.bots.external_harnesses.managed import ManagedSeatWorker
from sixnimmt.arena.bots.external_harnesses.transport import ControllerServer
from sixnimmt.arena.catalogue import CandidateConfig, FrozenModel
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.match import ActivityObserver, Observer, run_match
from sixnimmt.arena.players import PlayerConfig, resolve_players
from sixnimmt.arena.results import MatchResult
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.state import PlayerSeat


class SetupTimeout(RuntimeError):
    """Required external seats did not enter the waiting room in time."""


class TableConfig(FrozenModel):
    """Arena-style strategy definitions and one ordered lineup of catalogue keys."""

    catalogue: tuple[CandidateConfig, ...] = ()
    lineup: tuple[str, ...] = ()
    seed: int = 66
    rules: GameRules = Field(default_factory=GameRules)
    protocol: MatchProtocol = Field(default_factory=MatchProtocol)
    execution: RunConfig = Field(default_factory=RunConfig)

    @model_validator(mode="after")
    def check_table(self) -> "TableConfig":
        keys = [candidate.bot if candidate.key is None else candidate.key for candidate in self.catalogue]
        if len(set(keys)) != len(keys):
            msg = "duplicate catalogue key"
            raise ValueError(msg)
        for key in self.lineup:
            if key not in keys:
                msg = f"unknown catalogue key in lineup: {key}"
                raise ValueError(msg)
        if len(self.lineup) > 0:
            self._check_player_count(len(self.lineup))
        if self.execution.backend != "thread" or self.execution.concurrency != 1:
            msg = "tables require execution.backend='thread' and execution.concurrency=1"
            raise ValueError(msg)
        return self

    def _check_player_count(self, count: int) -> None:
        if not self.rules.min_players <= count <= self.rules.max_players:
            msg = f"a table requires between {self.rules.min_players} and {self.rules.max_players} players"
            raise ValueError(msg)

    def players(self, specifications: Sequence[str] | None = None) -> tuple[PlayerConfig, ...]:
        """Resolve fixed seats, or replace the lineup with CLI keys and seat shortcuts."""
        catalogue = {
            candidate.bot if candidate.key is None else candidate.key: candidate for candidate in self.catalogue
        }
        requested = self.lineup if specifications is None else specifications
        self._check_player_count(len(requested))
        players = []
        for specification in requested:
            candidate = catalogue.get(specification)
            if candidate is None:
                players.append(_seat_config(specification))
                continue
            label = specification if candidate.label is None else candidate.label
            players.append(PlayerConfig(bot=candidate.bot, options=candidate.options, display_name=label))
        return tuple(players)


def _seat_config(specification: str | PlayerConfig) -> PlayerConfig:
    if isinstance(specification, PlayerConfig):
        return specification
    name, separator, path = specification.partition(":")
    if name in ("codex", "claude") and separator != "":
        msg = "native seats use generated per-seat configuration and do not accept an options file"
        raise ValueError(msg)
    options = {} if separator == "" else json.loads(Path(path).read_text(encoding="utf-8"))
    return PlayerConfig(bot=name, options=options)


@dataclass
class TableSeat:
    seat: PlayerSeat
    bot: Bot
    session: SeatSession | None = None
    native_client: str | None = None
    driver: ManagedCommandDriver | None = None
    worker: ManagedSeatWorker | None = None


def create_seats(
    specifications: Sequence[str | PlayerConfig],
    *,
    seed: int,
    directory: Path,
    rules: GameRules,
    protocol: MatchProtocol,
    wait_timeout_seconds: float = 600,
    managed_timeout_seconds: float = 120,
    memory_enabled: bool = False,
    memory_max_chars: int = 4000,
) -> list[TableSeat]:
    """Construct an exact lineup without sampling or starting any processes."""
    if not rules.min_players <= len(specifications) <= rules.max_players:
        msg = f"a table requires between {rules.min_players} and {rules.max_players} players"
        raise ValueError(msg)
    result = []
    for index, specification in enumerate(specifications, start=1):
        player = _seat_config(specification)
        name = player.bot
        player_id = f"player_{index}"
        display_name = f"{name} {index}" if player.display_name is None else player.display_name
        seat = PlayerSeat(player_id=player_id, display_name=display_name)
        if name not in ("codex", "claude", "codex-headless", "claude-headless", "command"):
            resolved = resolve_players([player])[0]
            seat = seat.model_copy(update={"agent_metadata": resolved.metadata})
            result.append(TableSeat(seat, resolved.build(seed + index)))
            continue
        native = name in ("codex", "claude")
        if native and len(player.options) > 0:
            msg = "native seats use client configuration and do not accept bot options"
            raise ValueError(msg)
        session = SeatSession(
            player_id,
            seat.display_name,
            rules,
            protocol,
            wait_timeout_seconds=wait_timeout_seconds,
            memory_enabled=memory_enabled,
            memory_max_chars=memory_max_chars,
        )
        seat = seat.model_copy(
            update={
                "agent_metadata": {
                    **player.agent_metadata,
                    "harness": name,
                    "ownership": "attached" if native else "managed",
                    "context": "match" if native else "fresh",
                    "environment": "trusted_local",
                }
            }
        )
        driver = None
        worker = None
        bot = HarnessBot(session)
        if not native:
            profile = _command_profile(name, player.options, managed_timeout_seconds)
            recorded_options: dict[str, JsonValue] = {
                "timeout_seconds": profile.timeout_seconds,
                "max_output_bytes": profile.max_output_bytes,
            }
            if name != "command":
                recorded_options["model"] = profile.model
                recorded_options["reasoning_effort"] = profile.reasoning_effort
            seat = seat.model_copy(update={"agent_metadata": {**seat.agent_metadata, "bot_options": recorded_options}})
            driver = ManagedCommandDriver(profile, directory / "seats" / player_id / "work")
            worker = ManagedSeatWorker(session, driver)
            bot = ManagedHarnessBot(session, worker)
        result.append(TableSeat(seat, bot, session, name if native else None, driver, worker))
    return result


def _command_profile(name: str, options: dict[str, JsonValue], timeout: float) -> CommandProfile:
    if name == "command":
        settings = CommandOptions.model_validate(options)
        resolved_timeout = timeout if settings.timeout_seconds is None else settings.timeout_seconds
        return CommandProfile(tuple(settings.command), "command", resolved_timeout, settings.max_output_bytes)
    kind = "codex" if name == "codex-headless" else "claude"
    headless = HeadlessOptions.model_validate(options)
    resolved_timeout = timeout if headless.timeout_seconds is None else headless.timeout_seconds
    command = (kind,) if headless.command is None else tuple(headless.command)
    return CommandProfile(
        command,
        kind,
        resolved_timeout,
        headless.max_output_bytes,
        model=headless.model,
        reasoning_effort=headless.reasoning_effort,
    )


def wait_for_ready(
    seats: Sequence[TableSeat],
    timeout_seconds: float,
    stopped: Event,
) -> None:
    """Wait without running the arena or starting any decision deadline."""
    deadline = time.monotonic() + timeout_seconds
    pending = {seat.seat.player_id: seat for seat in seats if seat.session is not None}
    while len(pending) > 0:
        if stopped.is_set():
            msg = "table stopped during setup"
            raise SetupTimeout(msg)
        for player_id, seat in tuple(pending.items()):
            if seat.session is not None and seat.session.ready.is_set():
                del pending[player_id]
        if len(pending) == 0:
            return
        if time.monotonic() >= deadline:
            names = ", ".join(seat.seat.display_name for seat in pending.values())
            msg = f"setup timed out waiting for: {names}"
            raise SetupTimeout(msg)
        stopped.wait(0.05)


def _execute_match(
    seats: list[TableSeat],
    seed: int,
    rules: GameRules,
    protocol: MatchProtocol,
    config: RunConfig,
    observer: Observer | None,
    stopped: Event,
    outcome: Queue[MatchResult | BaseException],
    on_activity: ActivityObserver | None = None,
) -> None:
    try:
        result = run_match(
            [seat.bot for seat in seats],
            seed,
            match_id="table",
            rules=rules,
            protocol=protocol,
            config=config,
            seats=[seat.seat for seat in seats],
            observer=observer,
            on_activity=on_activity,
            stop_event=stopped,
        )
        outcome.put(result)
    except BaseException as error:
        outcome.put(error)


def _watch_match(
    seats: list[TableSeat],
    seed: int,
    rules: GameRules,
    protocol: MatchProtocol,
    config: RunConfig,
    observer: Observer | None,
    stopped: Event,
    report: Callable[[str], None],
    on_activity: ActivityObserver | None = None,
) -> MatchResult:
    outcome: Queue[MatchResult | BaseException] = Queue()
    thread = Thread(
        target=_execute_match,
        args=(seats, seed, rules, protocol, config, observer, stopped, outcome, on_activity),
        daemon=True,
    )
    thread.start()
    while thread.is_alive():
        try:
            thread.join(timeout=0.1)
        except KeyboardInterrupt:
            report("Stopping table...")
            stopped.set()
            for seat in seats:
                if seat.session is not None:
                    seat.session.stop()
                if seat.driver is not None:
                    seat.driver.cancel()
    result = outcome.get_nowait()
    if isinstance(result, BaseException):
        raise result
    return result


def _report_result(result: MatchResult, directory: Path, report: Callable[[str], None]) -> None:
    summary = {
        "outcome": result.outcome.value,
        "winners": result.winners,
        "reason": result.reason,
        "scores": {player.player_id: player.total_score for player in result.final_state.players},
    }
    (directory / "result.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    report(f"Match {result.outcome.value}. Winners: {', '.join(result.winners)}")
    report(f"Trace: {directory / 'traces' / 'table.jsonl'}")


def _serve_table(
    seats: list[TableSeat],
    directory: Path,
    seed: int,
    rules: GameRules,
    protocol: MatchProtocol,
    config: RunConfig,
    setup_timeout: float,
    auto_start: bool,
    retain_seconds: float,
    report: Callable[[str], None],
    confirm_start: Callable[[], bool],
    observer: Observer | None,
    on_activity: ActivityObserver | None = None,
) -> MatchResult:
    stopped = Event()
    sessions = [seat.session for seat in seats if seat.session is not None and seat.native_client is not None]
    workers = [seat.worker for seat in seats if seat.worker is not None]
    with ExitStack() as stack:
        _open_connections(seats, directory, stack, report)
        stack.callback(_close_table, seats, workers, stopped)
        for worker in workers:
            worker.start()
        wait_for_ready(seats, setup_timeout, stopped)
        if not auto_start and not confirm_start():
            msg = "table start cancelled"
            raise SetupTimeout(msg)
        result = _watch_match(seats, seed, rules, protocol, config, observer, stopped, report, on_activity)
        _report_result(result, directory, report)
        for worker in workers:
            worker.close()
        if len(sessions) > 0 and retain_seconds > 0:
            _retain_results(retain_seconds, report)
        return result


def _open_connections(seats: list[TableSeat], directory: Path, stack: ExitStack, report: Callable[[str], None]) -> None:
    sessions = [seat.session for seat in seats if seat.session is not None and seat.native_client is not None]
    if len(sessions) == 0:
        return
    server = stack.enter_context(ControllerServer(sessions))
    for seat in seats:
        if seat.native_client is not None:
            if seat.session is None:
                msg = "only external seats have connection bundles"
                raise ValueError(msg)
            workspace = write_connection_bundle(seat.session, directory, server.endpoint, seat.native_client)
            report(f"{seat.seat.display_name}: open a separate terminal and run {workspace / 'launch.sh'}")


def _close_table(seats: list[TableSeat], workers: list[ManagedSeatWorker], stopped: Event) -> None:
    stopped.set()
    for seat in seats:
        if seat.session is not None:
            seat.session.stop()
            seat.session.close(None)
    for worker in workers:
        if worker.thread.ident is not None:
            worker.close()


def _retain_results(seconds: float, report: Callable[[str], None]) -> None:
    report(f"Keeping seat results available for {seconds:g} seconds. Press Ctrl-C to close.")
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            # An operator stop ends play, while this independent window keeps
            # terminal replies and recovery available. A second Ctrl-C closes it.
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        return


def run_table(
    seatspecs: Sequence[str | PlayerConfig],
    directory: Path,
    seed: int,
    rules: GameRules,
    protocol: MatchProtocol,
    config: RunConfig,
    *,
    setup_timeout: float = 600,
    auto_start: bool = False,
    retain_seconds: float = 30,
    wait_timeout_seconds: float = 600,
    managed_timeout_seconds: float = 120,
    memory_enabled: bool = False,
    memory_max_chars: int = 4000,
    report: Callable[[str], None],
    confirm_start: Callable[[], bool],
    observer: Observer | None = None,
    on_activity: ActivityObserver | None = None,
) -> MatchResult:
    """Run an explicit table with caller-provided reporting and start confirmation."""
    directory = directory.resolve()
    seats = create_seats(
        seatspecs,
        seed=seed,
        directory=directory,
        rules=rules,
        protocol=protocol,
        wait_timeout_seconds=wait_timeout_seconds,
        managed_timeout_seconds=managed_timeout_seconds,
        memory_enabled=memory_enabled,
        memory_max_chars=memory_max_chars,
    )
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    return _serve_table(
        seats,
        directory,
        seed,
        rules,
        protocol,
        config,
        setup_timeout,
        auto_start,
        retain_seconds,
        report,
        confirm_start,
        observer,
        on_activity,
    )
