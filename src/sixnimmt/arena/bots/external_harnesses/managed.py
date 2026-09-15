"""Own fresh harness invocations within the arena bot lifecycle."""

import json
import time
from datetime import UTC, datetime
from threading import Event, Thread
from typing import Any

from sixnimmt.arena.bots.external_harnesses.broker import PLAY_INSTRUCTIONS, SeatSession
from sixnimmt.arena.bots.external_harnesses.drivers import DriverError, DriverFormatError, ManagedCommandDriver
from sixnimmt.arena.bots.external_harnesses.protocol import HarnessError, decision_proposal_schema
from sixnimmt.arena.bots.lifecycle import DecisionDeadlineExceeded


class ManagedSeatWorker:
    """Translate broker decisions into fresh CLI invocations off the match thread."""

    def __init__(self, session: SeatSession, driver: ManagedCommandDriver) -> None:
        self.session = session
        self.driver = driver
        self.stopped = Event()
        self.thread = Thread(target=self._run, name=f"harness-{session.player_id}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stopped.set()
        self.driver.cancel()
        if self.thread.ident is not None:
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                msg = "managed worker did not stop within its cleanup bound"
                raise DriverError(msg)

    def _run(self) -> None:
        try:
            self._play_loop()
        except Exception as error:
            if self.stopped.is_set():
                return
            if isinstance(error, DriverError) and str(error) == "managed_decision_timeout":
                error = DecisionDeadlineExceeded("managed_decision_timeout")
            self.session.fail(error)

    def _play_loop(self) -> None:
        info = self.session.get_game_info(self.session.session_id)
        instructions = info["instructions"].removesuffix("\n\n" + PLAY_INSTRUCTIONS)
        instructions += (
            "\n\nReturn one final JSON proposal for the offered decision. Include protocol_version, "
            "session_id, decision_id, submission_id, view_id, actions and memory. Copy the offered IDs "
            "and choose a fresh unique submission_id. The arena validates the complete proposal. "
            "This invocation ends after the proposal; another invocation handles the next decision. "
            "Only an enabled arena-accepted notebook persists across invocations."
        )
        info["instructions"] = instructions
        response = self.session.play(self.session.session_id, cancel=self.stopped)
        while not self.stopped.is_set():
            if response["status"] == "terminal":
                return
            if response["status"] == "wait_expired":
                response = self.session.play(self.session.session_id, cancel=self.stopped)
                continue
            offer = response.get("offer")
            if response["status"] != "decision" or not isinstance(offer, dict):
                msg = "managed worker received an unexpected broker response"
                raise DriverError(msg)
            offer = {**offer, "instructions": instructions}
            response = self._submit_decision(offer, info)

    def _submit_decision(self, offer: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
        schema = decision_proposal_schema(
            offer, memory_enabled=info["memory_enabled"], memory_max_chars=info["memory_max_chars"]
        )
        decision_info = {**info, "proposal_schema": schema}
        deadline = time.monotonic() + self.driver.profile.timeout_seconds
        arena_deadline = offer.get("deadline")
        if isinstance(arena_deadline, str):
            remaining = (datetime.fromisoformat(arena_deadline) - datetime.now(UTC)).total_seconds()
            deadline = min(deadline, time.monotonic() + remaining)
        request_offer = offer
        for attempt in range(2):
            context = {
                "decision_id": offer["decision_id"],
                "view_id": offer["view_id"],
                "attempt": attempt + 1,
                "invocation": self.driver.invocations + 1,
            }
            self.driver.emit_trace({
                "type": "decision_request",
                **context,
                **_bounded_data("offer", request_offer, 65_536),
                "proposal_schema": schema,
            })
            proposal: dict[str, Any] | None = None
            try:
                proposal = self.driver.invoke(request_offer, decision_info, schema, deadline=deadline)
                self.driver.emit_trace({"type": "proposal", **context, **_bounded_data("proposal", proposal, 65_536)})
                return self.session.play(self.session.session_id, proposal, cancel=self.stopped)
            except (DriverFormatError, HarnessError) as error:
                repairable = isinstance(error, DriverFormatError) or error.code in (
                    "invalid_proposal",
                    "invalid_memory",
                    "unavailable_action",
                )
                feedback: dict[str, Any] = {
                    "code": error.code if isinstance(error, HarnessError) else "invalid_final_json",
                    "message": str(error),
                }
                if proposal is not None:
                    feedback.update(_bounded_data("previous_proposal", proposal, 16_384))
                self.driver.emit_trace({
                    "type": "proposal_rejected",
                    **context,
                    "error": feedback,
                    "repair_will_follow": attempt == 0 and repairable,
                })
                if attempt == 1 or not repairable:
                    raise
                request_offer = {**offer, "protocol_error": feedback}
                self.driver.emit_trace({"type": "protocol_repair", **context, "error": feedback})
        msg = "managed proposal repair limit exhausted"
        raise DriverError(msg)


def _bounded_data(name: str, value: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Keep private diagnostics useful without unbounded prompts or trace records."""
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if len(encoded) <= max_bytes:
        return {name: value}
    # A JSON excerpt is escaped again by the enclosing trace/prompt JSON.
    excerpt_bytes = min(max_bytes, 16_384)
    return {f"{name}_excerpt": encoded[:excerpt_bytes], f"{name}_truncated": True}
