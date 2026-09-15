"""Prepare and retain optional live sessions around any scheduled match."""

import json
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Event
from time import monotonic
from typing import Literal

from pydantic import JsonValue

from sixnimmt.arena.bots.base import Bot
from sixnimmt.arena.bots.external import CommandOptions, HarnessBot, HeadlessOptions, ManagedHarnessBot
from sixnimmt.arena.bots.external_harnesses.broker import SeatSession
from sixnimmt.arena.bots.external_harnesses.connection import write_connection_bundle
from sixnimmt.arena.bots.external_harnesses.drivers import CommandProfile, ManagedCommandDriver
from sixnimmt.arena.bots.external_harnesses.managed import ManagedSeatWorker
from sixnimmt.arena.bots.external_harnesses.transport import ControllerServer
from sixnimmt.arena.config import SessionOptions
from sixnimmt.arena.decisions import StopSignal
from sixnimmt.arena.match import _BotLifecycle
from sixnimmt.arena.players import PlayerConfig, ResolvedStrategy, resolve_players
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.state import PlayerSeat


class SeatConstructionError(RuntimeError):
    """A zero-based lineup seat failed; previously built seats are already closed."""

    def __init__(self, seat: int, cause: Exception) -> None:
        self.seat = seat
        self.cause = cause
        super().__init__(f"cannot construct seat {seat + 1}: {cause}")


type NativeClient = Literal["codex", "claude"]


@dataclass(frozen=True)
class AttachedSeatDescriptor:
    client: NativeClient
    ownership: Literal["attached"] = "attached"


@dataclass(frozen=True)
class ManagedSeatDescriptor:
    client: Literal["command", "codex", "claude"]
    ownership: Literal["managed"] = "managed"


type ExternalSeatDescriptor = AttachedSeatDescriptor | ManagedSeatDescriptor

EXTERNAL_SEATS: dict[str, ExternalSeatDescriptor] = {
    "codex": AttachedSeatDescriptor("codex"),
    "claude": AttachedSeatDescriptor("claude"),
    "codex-headless": ManagedSeatDescriptor("codex"),
    "claude-headless": ManagedSeatDescriptor("claude"),
    "command": ManagedSeatDescriptor("command"),
}


@dataclass(frozen=True)
class ResolvedExternalSeat:
    """Validated construction settings and separate safe diagnostic provenance."""

    descriptor: ExternalSeatDescriptor
    options: dict[str, JsonValue]
    recorded_options: dict[str, JsonValue]
    metadata: dict[str, JsonValue]
    deterministic: Literal[False] = False


def resolve_external_options(name: str, options: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Validate an external definition without creating a session or workspace."""
    descriptor = EXTERNAL_SEATS.get(name)
    if descriptor is None:
        msg = f"unknown external strategy {name!r}"
        raise ValueError(msg)
    if isinstance(descriptor, AttachedSeatDescriptor):
        if len(options) > 0:
            msg = "native seats use client configuration and do not accept bot options"
            raise ValueError(msg)
        return {}
    # CommandProfile also checks constraints shared with direct driver callers.
    _command_profile(name, options, 120)
    if descriptor.client == "command":
        return CommandOptions.model_validate(options).model_dump(mode="json")
    return HeadlessOptions.model_validate(options).model_dump(mode="json")


def resolve_external_seat(name: str, options: dict[str, JsonValue]) -> ResolvedExternalSeat:
    """Freeze external construction separately from seat identity and session limits."""
    validated = resolve_external_options(name, options)
    descriptor = EXTERNAL_SEATS[name]
    recorded = {key: value for key, value in validated.items() if key != "command"}
    metadata: dict[str, JsonValue] = {
        "harness": name,
        "ownership": descriptor.ownership,
        "context": "match" if isinstance(descriptor, AttachedSeatDescriptor) else "fresh",
        "environment": "trusted_local",
    }
    if isinstance(descriptor, ManagedSeatDescriptor):
        metadata["bot_options"] = recorded
    return ResolvedExternalSeat(descriptor, validated, recorded, metadata)


@dataclass
class PreparedSeat:
    """One constructed seat; the session supervisor owns external resources."""

    seat: PlayerSeat
    bot: Bot
    session: SeatSession | None = None
    native_client: NativeClient | None = None
    driver: ManagedCommandDriver | None = None
    worker: ManagedSeatWorker | None = None
    descriptor: ExternalSeatDescriptor | None = None

    @property
    def ownership(self) -> Literal["registered", "attached", "managed"]:
        return "registered" if self.descriptor is None else self.descriptor.ownership

    @property
    def requires_readiness(self) -> bool:
        return self.session is not None

    @property
    def process_compatible(self) -> bool:
        # A live external session belongs to its local controller/worker thread.
        return self.session is None


def parse_seat(specification: str | PlayerConfig) -> PlayerConfig:
    """Parse a command-line seat shortcut into explicit construction settings."""
    if isinstance(specification, PlayerConfig):
        return specification
    name, separator, path = specification.partition(":")
    descriptor = EXTERNAL_SEATS.get(name)
    if isinstance(descriptor, AttachedSeatDescriptor) and separator != "":
        msg = "native seats use generated per-seat configuration and do not accept an options file"
        raise ValueError(msg)
    options = {} if separator == "" else json.loads(Path(path).read_text(encoding="utf-8"))
    return PlayerConfig(bot=name, options=options)


def prepare_seats(
    players: Sequence[PlayerConfig],
    *,
    seed: int,
    directory: Path,
    rules: GameRules,
    protocol: MatchProtocol,
    session_options: SessionOptions,
    bot_seeds: Sequence[int] | None = None,
    strategies: Sequence[ResolvedStrategy | None] | None = None,
) -> list[PreparedSeat]:
    """Construct a normalized lineup without starting any external processes.

    Omitted bot seeds use the match seed plus the one-based seat index.
    Scheduled runs supply their own explicit seeds.
    """
    if not rules.min_players <= len(players) <= rules.max_players:
        msg = f"a lineup requires between {rules.min_players} and {rules.max_players} players"
        raise ValueError(msg)
    seeds = tuple(seed + index for index in range(1, len(players) + 1)) if bot_seeds is None else tuple(bot_seeds)
    if len(seeds) != len(players) or any(type(value) is not int for value in seeds):
        msg = "bot_seeds must contain one integer seed per seat"
        raise ValueError(msg)
    definitions = (None,) * len(players) if strategies is None else tuple(strategies)
    if len(definitions) != len(players):
        msg = "strategies must contain one resolved definition per seat"
        raise ValueError(msg)
    if strategies is not None:
        for player, definition in zip(players, definitions, strict=True):
            external = player.bot in EXTERNAL_SEATS
            if external != (definition is None):
                msg = "registered seats require resolved strategies; external seats require session construction"
                raise ValueError(msg)
    result: list[PreparedSeat] = []
    try:
        for index, (player, bot_seed, strategy) in enumerate(zip(players, seeds, definitions, strict=True), start=1):
            try:
                seat = _create_seat(player, index, bot_seed, directory, rules, protocol, session_options, strategy)
            except Exception as error:
                raise SeatConstructionError(index - 1, error) from error
            result.append(seat)
    except BaseException:
        _BotLifecycle().close_unstarted([seat.bot for seat in result], [seat.seat for seat in result])
        raise
    return result


def _create_seat(
    player: PlayerConfig,
    index: int,
    bot_seed: int,
    directory: Path,
    rules: GameRules,
    protocol: MatchProtocol,
    session_options: SessionOptions,
    strategy: ResolvedStrategy | None,
) -> PreparedSeat:
    name = player.bot
    player_id = f"player_{index}"
    display_name = f"{name} {index}" if player.display_name is None else player.display_name
    descriptor = EXTERNAL_SEATS.get(name)
    if descriptor is None:
        if strategy is None:
            resolved = resolve_players([player])[0]
            metadata = resolved.metadata
            strategy = resolved.strategy
        else:
            metadata = {**strategy.metadata, **player.agent_metadata}
            metadata["bot_options"] = strategy.recorded_options
        seat = PlayerSeat(player_id=player_id, display_name=display_name, agent_metadata=metadata)
        return PreparedSeat(seat, strategy.build(bot_seed))
    if isinstance(descriptor, AttachedSeatDescriptor) and len(player.options) > 0:
        msg = "native seats use client configuration and do not accept bot options"
        raise ValueError(msg)
    profile = (
        _command_profile(name, player.options, session_options.managed_timeout_seconds)
        if isinstance(descriptor, ManagedSeatDescriptor)
        else None
    )
    metadata: dict[str, JsonValue] = {
        **player.agent_metadata,
        "harness": name,
        "ownership": descriptor.ownership,
        "context": "match" if isinstance(descriptor, AttachedSeatDescriptor) else "fresh",
        "environment": "trusted_local",
    }
    if profile is not None:
        metadata["bot_options"] = _recorded_options(profile)
    seat = PlayerSeat(player_id=player_id, display_name=display_name, agent_metadata=metadata)
    session = SeatSession(
        player_id,
        display_name,
        rules,
        protocol,
        wait_timeout_seconds=session_options.wait_timeout_seconds,
        memory_enabled=session_options.memory_enabled,
        memory_max_chars=session_options.memory_max_chars,
    )
    if isinstance(descriptor, AttachedSeatDescriptor):
        return PreparedSeat(seat, HarnessBot(session), session, descriptor.client, descriptor=descriptor)
    if profile is None:
        msg = "managed seat construction requires a validated command profile"
        raise RuntimeError(msg)
    driver = ManagedCommandDriver(profile, directory / "seats" / player_id / "work")
    worker = ManagedSeatWorker(session, driver)
    bot = ManagedHarnessBot(session, worker)
    return PreparedSeat(seat, bot, session, driver=driver, worker=worker, descriptor=descriptor)


def _recorded_options(profile: CommandProfile) -> dict[str, JsonValue]:
    """Record settings without command arguments or environment values."""
    recorded: dict[str, JsonValue] = {
        "timeout_seconds": profile.timeout_seconds,
        "max_output_bytes": profile.max_output_bytes,
    }
    if profile.kind != "command":
        recorded["model"] = profile.model
        recorded["reasoning_effort"] = profile.reasoning_effort
    return recorded


def _command_profile(name: str, options: dict[str, JsonValue], timeout: float) -> CommandProfile:
    descriptor = EXTERNAL_SEATS.get(name)
    if not isinstance(descriptor, ManagedSeatDescriptor):
        msg = f"seat {name!r} is not a managed external strategy"
        raise ValueError(msg)  # noqa: TRY004 -- Reject an unsupported registry name, not a caller type.
    if descriptor.client == "command":
        settings = CommandOptions.model_validate(options)
        resolved_timeout = timeout if settings.timeout_seconds is None else settings.timeout_seconds
        return CommandProfile(tuple(settings.command), "command", resolved_timeout, settings.max_output_bytes)
    headless = HeadlessOptions.model_validate(options)
    resolved_timeout = timeout if headless.timeout_seconds is None else headless.timeout_seconds
    command = (descriptor.client,) if headless.command is None else tuple(headless.command)
    return CommandProfile(
        command,
        descriptor.client,
        resolved_timeout,
        headless.max_output_bytes,
        model=headless.model,
        reasoning_effort=headless.reasoning_effort,
    )


class SetupTimeout(RuntimeError):
    """A live session did not become ready or its operator cancelled startup."""


class ConfirmationDispatcher:
    """Run an interactive callback in the caller while a ready worker waits."""

    def __init__(self, confirm: Callable[[], bool], stopped: StopSignal) -> None:
        self.confirm = confirm
        self.stopped = stopped
        self.requests: Queue[Future[bool]] = Queue()

    def request(self) -> bool:
        response: Future[bool] = Future()
        self.requests.put(response)
        while not self.stopped.is_set():
            try:
                return response.result(timeout=0.05)
            except TimeoutError:
                continue
        return False

    def poll(self) -> None:
        while True:
            try:
                response = self.requests.get_nowait()
            except Empty:
                return
            if self.stopped.is_set():
                response.set_result(False)
                continue
            try:
                accepted = self.confirm()
            except BaseException:
                response.set_result(False)
                raise
            response.set_result(accepted)


def wait_for_ready(seats: Sequence[PreparedSeat], timeout_seconds: float, stopped: StopSignal) -> None:
    deadline = monotonic() + timeout_seconds
    pending = [seat for seat in seats if seat.requires_readiness]
    while len(pending) > 0:
        if stopped.is_set():
            msg = "run stopped during setup"
            raise SetupTimeout(msg)
        pending = [seat for seat in pending if seat.session is not None and not seat.session.ready.is_set()]
        if len(pending) == 0:
            return
        if monotonic() >= deadline:
            names = ", ".join(seat.seat.name_or_id for seat in pending)
            msg = f"setup timed out waiting for: {names}"
            raise SetupTimeout(msg)
        stopped.wait(0.05)


class SessionSupervisor:
    def __init__(
        self,
        seats: Sequence[PreparedSeat],
        directory: Path,
        options: SessionOptions,
        lifecycle: _BotLifecycle,
        stopped: StopSignal,
        report: Callable[[str], None] | None,
        confirm_start: Callable[[], bool] | None,
        watch: bool,
        closed: StopSignal,
    ) -> None:
        self.closed = closed
        self.seats = seats
        self.directory = directory
        self.options = options
        self.lifecycle = lifecycle
        self.stopped = stopped
        self.report = report
        self.confirm_start = confirm_start
        self.watch = watch
        self.connections = ExitStack()

    def open(self) -> None:
        attached = [seat.session for seat in self.seats if seat.session is not None and seat.native_client is not None]
        if len(attached) > 0:
            server = self.connections.enter_context(ControllerServer(attached))
            for seat in self.seats:
                if seat.native_client is not None and seat.session is not None:
                    workspace = write_connection_bundle(
                        seat.session, self.directory, server.endpoint, seat.native_client
                    )
                    self.message(f"{seat.seat.name_or_id}: open a separate terminal and run {workspace / 'launch.sh'}")
        for seat in self.seats:
            if seat.worker is not None:
                seat.worker.start()
        wait_for_ready(self.seats, self.options.setup_timeout, self.stopped)
        if (self.watch or len(attached) > 0) and not self.options.auto_start:
            if self.confirm_start is None:
                msg = "interactive start requires a confirmation callback or session.auto_start=true"
                raise ValueError(msg)
            if not self.confirm_start():
                msg = "run start cancelled"
                raise SetupTimeout(msg)

    def retain(self) -> None:
        if not any(seat.native_client is not None for seat in self.seats) or self.options.retain_seconds == 0:
            return
        self.message(
            f"Keeping seat results available for {self.options.retain_seconds:g} seconds. Press Ctrl-C to close."
        )
        # Retention is independent of the match stop event, allowing a pending
        # attached client to receive its terminal reply after an operator stop.
        try:
            self.closed.wait(self.options.retain_seconds)
        except KeyboardInterrupt:
            return

    def message(self, text: str) -> None:
        if self.report is not None:
            self.report(text)

    def close(self) -> None:
        self.lifecycle.close_unstarted([seat.bot for seat in self.seats], [seat.seat for seat in self.seats])
        for seat in self.seats:
            if seat.session is not None:
                seat.session.stop()
        self.connections.close()


@contextmanager
def supervise_sessions(
    seats: Sequence[PreparedSeat],
    directory: Path,
    options: SessionOptions,
    lifecycle: _BotLifecycle,
    stopped: StopSignal,
    *,
    report: Callable[[str], None] | None = None,
    confirm_start: Callable[[], bool] | None = None,
    watch: bool = False,
    closed: StopSignal | None = None,
) -> Iterator[SessionSupervisor]:
    supervisor = SessionSupervisor(
        seats,
        directory,
        options,
        lifecycle,
        stopped,
        report,
        confirm_start,
        watch,
        Event() if closed is None else closed,
    )
    try:
        supervisor.open()
        yield supervisor
    finally:
        supervisor.close()
