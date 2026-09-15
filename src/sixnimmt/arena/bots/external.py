"""Bot implementations for attached and managed external harnesses."""

from collections.abc import Callable
from typing import Any

from pydantic import Field

from sixnimmt.arena.bots.base import ActionBatch, BotOptions, Rejection
from sixnimmt.arena.bots.external_harnesses.broker import SeatSession
from sixnimmt.arena.bots.external_harnesses.managed import ManagedSeatWorker
from sixnimmt.arena.bots.lifecycle import BotContext, BotMatchEnd, DecisionContext, DecisionOutcome
from sixnimmt.engine.views import MatchView


class CommandOptions(BotOptions):
    """Operator-supplied command implementing the managed JSON contract."""

    command: list[str] = Field(min_length=1)
    timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_output_bytes: int = Field(default=1_048_576, ge=1)


class HeadlessOptions(BotOptions):
    """Model and invocation settings shared by headless Codex and Claude seats."""

    model: str | None = Field(default=None, min_length=1)
    reasoning_effort: str | None = Field(default=None, min_length=1)
    command: list[str] | None = Field(default=None, min_length=1)
    timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_output_bytes: int = Field(default=1_048_576, ge=1)


class HarnessBot:
    def __init__(self, session: SeatSession) -> None:
        self.session = session

    def act(self, view: MatchView, rejection: Rejection | None = None) -> ActionBatch:
        return self.session.decide(view, rejection)

    def lifecycle_identity(self) -> object:
        return self.session

    def start(self, context: BotContext) -> None:
        self.session.start(context)

    def set_decision_context(self, context: DecisionContext) -> None:
        self.session.set_decision_context(context)

    def settle_decision(self, outcome: DecisionOutcome) -> None:
        self.session.settle_decision(outcome)

    def accept_batch(self, batch: ActionBatch) -> None:
        self.session.accept_batch(batch)

    def cancel(self, reason: str) -> None:
        self.session.cancel(reason)

    def close(self, result: BotMatchEnd | None) -> None:
        self.session.close(result)

    def stats(self) -> dict[str, Any]:
        return self.session.stats()


class ManagedHarnessBot(HarnessBot):
    """Keep owned-process cleanup inside the arena's bot lifecycle."""

    def __init__(self, session: SeatSession, worker: ManagedSeatWorker) -> None:
        super().__init__(session)
        self.worker = worker

    def set_trace(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        """Record private invocation output through the arena's model trace sink."""
        self.worker.driver.set_trace(callback)

    def start(self, context: BotContext) -> None:
        super().start(context)
        if self.worker.thread.ident is None:
            self.worker.start()

    def cancel(self, reason: str) -> None:
        super().cancel(reason)
        self.worker.driver.cancel()

    def close(self, result: BotMatchEnd | None) -> None:
        super().close(result)
        self.worker.close()

    def stats(self) -> dict[str, object]:
        return {
            **super().stats(),
            "driver": self.worker.driver.profile.kind,
            "context": "fresh",
            "invocations": self.worker.driver.invocations,
            "managed_timeout_seconds": self.worker.driver.profile.timeout_seconds,
        }
