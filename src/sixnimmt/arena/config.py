"""Validated harness settings, independent of game rules."""

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from sixnimmt.engine.rules import MatchProtocol

type SchedulerName = Literal["sequential", "round_robin"]
type Backend = Literal["thread", "process"]
type PositiveInt = Annotated[int, Field(strict=True, gt=0)]
type NonnegativeInt = Annotated[int, Field(strict=True, ge=0)]
type PositiveSeconds = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]


@dataclass(frozen=True)
class MatchLimits:
    scheduler: SchedulerName
    match_action_limit: int
    play_action_limit: int | None
    decision_rejection_limit: int
    decision_timeout_seconds: float | None


@dataclass(frozen=True)
class RunLimits:
    backend: Backend
    concurrency: int
    stop_on_failure: bool
    max_abandoned_decisions: int


@dataclass(frozen=True)
class RecordingPolicy:
    trace_dir: Path | None
    retain_decision_samples: bool = False


@dataclass(frozen=True)
class ExecutionSettings:
    match: MatchLimits
    run: RunLimits
    recording: RecordingPolicy


@dataclass(frozen=True)
class RunConfig:
    """Execution inputs; resolve_settings separates match, run, and recording limits."""

    scheduler: SchedulerName | None = None
    match_action_limit: PositiveInt = 10_000
    play_action_limit: PositiveInt | None = None
    decision_rejection_limit: PositiveInt = 8
    decision_timeout_seconds: PositiveSeconds | None = None
    max_abandoned_decisions: NonnegativeInt | None = None
    concurrency: PositiveInt = 1
    stop_on_failure: Annotated[bool, Field(strict=True)] = False
    trace_dir: Path | None = None
    backend: Backend = "thread"


def resolve(config: RunConfig, protocol: MatchProtocol) -> RunConfig:
    """Resolve mode-dependent defaults and reject invalid harness settings."""
    _validate_limits(config)
    scheduler = config.scheduler
    if scheduler is None:
        scheduler = "round_robin" if protocol.communication_enabled else "sequential"
    if scheduler not in ("sequential", "round_robin"):
        msg = f"unknown scheduler {scheduler!r}; available: sequential, round_robin"
        raise ValueError(msg)
    if scheduler == "sequential" and protocol.communication_enabled:
        msg = "sequential scheduling would starve later seats under communication; use round_robin"
        raise ValueError(msg)
    play_limit = config.play_action_limit
    if play_limit is None and protocol.communication_enabled:
        play_limit = 200
    abandoned_limit = config.max_abandoned_decisions
    if abandoned_limit is None:
        abandoned_limit = 4 * config.concurrency
    resolved = replace(
        config, scheduler=scheduler, play_action_limit=play_limit, max_abandoned_decisions=abandoned_limit
    )
    _validate_limits(resolved)
    return resolved


def _validate_limits(config: RunConfig) -> None:
    if config.backend not in ("thread", "process"):
        msg = f"unknown backend {config.backend!r}; available: thread, process"
        raise ValueError(msg)
    for name in ("match_action_limit", "decision_rejection_limit", "concurrency"):
        value = getattr(config, name)
        if type(value) is not int or value < 1:
            msg = f"{name} must be a positive integer"
            raise ValueError(msg)
    play_limit = config.play_action_limit
    if play_limit is not None and (type(play_limit) is not int or play_limit < 1):
        msg = "play_action_limit must be a positive integer"
        raise ValueError(msg)
    abandoned_limit = config.max_abandoned_decisions
    if abandoned_limit is not None and (type(abandoned_limit) is not int or abandoned_limit < 0):
        msg = "max_abandoned_decisions must be a nonnegative integer"
        raise ValueError(msg)
    if type(config.stop_on_failure) is not bool:
        msg = "stop_on_failure must be a boolean"
        raise ValueError(msg)
    timeout = config.decision_timeout_seconds
    if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
        msg = "decision_timeout_seconds must be finite and positive"
        raise ValueError(msg)


def resolved_abandoned_limit(config: RunConfig) -> int:
    """Worker settings must have passed resolve(); zero is a valid bound."""
    if config.max_abandoned_decisions is None:
        msg = "arena configuration has not resolved the abandoned-decision limit"
        raise ValueError(msg)
    return config.max_abandoned_decisions


def resolve_settings(config: RunConfig, protocol: MatchProtocol) -> ExecutionSettings:
    """Validate once at a runtime boundary and expose nonoptional scoped settings."""
    config = resolve(config, protocol)
    scheduler = config.scheduler
    if scheduler is None:
        msg = "scheduler was not resolved"
        raise ValueError(msg)
    return ExecutionSettings(
        match=MatchLimits(
            scheduler,
            config.match_action_limit,
            config.play_action_limit,
            config.decision_rejection_limit,
            config.decision_timeout_seconds,
        ),
        run=RunLimits(config.backend, config.concurrency, config.stop_on_failure, resolved_abandoned_limit(config)),
        recording=RecordingPolicy(config.trace_dir),
    )


def validate_duration(name: str, value: float, *, allow_zero: bool = False) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        msg = f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}"
        raise ValueError(msg)


@dataclass(frozen=True)
class SessionOptions:
    """External-session policy; independent of engine rules and decision limits."""

    setup_timeout: PositiveSeconds = 600
    auto_start: Annotated[bool, Field(strict=True)] = False
    retain_seconds: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)] = 30
    wait_timeout_seconds: PositiveSeconds = 600
    managed_timeout_seconds: PositiveSeconds = 120
    memory_enabled: Annotated[bool, Field(strict=True)] = False
    memory_max_chars: Annotated[int, Field(strict=True, ge=1, le=16_000)] = 4000

    def __post_init__(self) -> None:
        validate_duration("setup_timeout", self.setup_timeout)
        validate_duration("wait_timeout_seconds", self.wait_timeout_seconds)
        validate_duration("managed_timeout_seconds", self.managed_timeout_seconds)
        validate_duration("retain_seconds", self.retain_seconds, allow_zero=True)
        if type(self.memory_max_chars) is not int or not 1 <= self.memory_max_chars <= 16_000:
            msg = "memory_max_chars must be an integer between 1 and 16000"
            raise ValueError(msg)
        if type(self.auto_start) is not bool or type(self.memory_enabled) is not bool:
            msg = "auto_start and memory_enabled must be booleans"
            raise ValueError(msg)


@dataclass(frozen=True)
class DisplayOptions:
    watch: Annotated[bool, Field(strict=True)] = False
    animation: Annotated[bool, Field(strict=True)] = True
    commentary: Annotated[bool, Field(strict=True)] = True
    quiet: Annotated[bool, Field(strict=True)] = False


@dataclass(frozen=True)
class RecordingOptions:
    output_dir: Path | None = None
    trace: Annotated[bool, Field(strict=True)] = False
