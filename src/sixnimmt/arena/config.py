"""Validated harness settings, independent of game rules."""

import math
from dataclasses import dataclass, replace
from pathlib import Path

from sixnimmt.engine.rules import MatchProtocol


@dataclass(frozen=True)
class RunConfig:
    scheduler: str | None = None
    match_action_limit: int = 10_000
    play_action_limit: int | None = None
    decision_rejection_limit: int = 8
    decision_timeout_seconds: float | None = None
    max_abandoned_decisions: int | None = None
    concurrency: int = 1
    stop_on_failure: bool = False
    trace_dir: Path | None = None
    backend: str = "thread"


def resolve(config: RunConfig, protocol: MatchProtocol) -> RunConfig:
    """Resolve mode-dependent defaults and reject invalid harness settings."""
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
    for name in ("match_action_limit", "play_action_limit", "decision_rejection_limit", "concurrency"):
        value = getattr(config, name)
        if value is not None and (type(value) is not int or value < 1):
            msg = f"{name} must be a positive integer"
            raise ValueError(msg)
    abandoned_limit = config.max_abandoned_decisions
    if type(abandoned_limit) is not int or abandoned_limit < 0:
        msg = "max_abandoned_decisions must be a nonnegative integer"
        raise ValueError(msg)
    timeout = config.decision_timeout_seconds
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        msg = "decision_timeout_seconds must be finite and positive"
        raise ValueError(msg)
