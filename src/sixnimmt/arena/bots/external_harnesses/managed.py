"""Own fresh harness invocations within the arena bot lifecycle."""

import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from typing import Any

from sixnimmt.arena.bots.external_harnesses.broker import SeatSession
from sixnimmt.arena.bots.external_harnesses.drivers import DriverError, DriverFormatError, ManagedCommandDriver
from sixnimmt.arena.bots.external_harnesses.messages import DecisionOffer, GameInfo, PlayReply
from sixnimmt.arena.bots.external_harnesses.protocol import (
    HarnessError,
    managed_proposal_schema,
    parse_managed_proposal,
)


class ManagedSeatWorker:
    """Translate broker decisions into fresh CLI invocations off the match thread."""

    def __init__(self, session: SeatSession, driver: ManagedCommandDriver) -> None:
        self.session = session
        self.driver = driver
        self.stopped = Event()
        self._close_lock = Lock()
        self._closed = False
        self.thread = Thread(target=self._run, name=f"harness-{session.player_id}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
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
            self.session.fail(error)

    def _play_loop(self) -> None:
        info = self.session.get_game_info(self.session.session_id)
        instructions = self.session.instructions(
            "Return one final JSON proposal for the offered decision. Include protocol_version, "
            "session_id, decision_id, submission_id, view_id, actions and memory. Copy the offered IDs "
            "and choose a fresh unique submission_id. The arena validates the complete proposal. "
            "Add an explanation of your proposed move in one or two concise sentences, at most 1000 characters. "
            "Give a short decision summary grounded in the current game situation. "
            "This is private commentary for the operator and does not send a message to other players. "
            "Use explanation: null when no explanation is available. "
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
            if response["status"] != "decision":
                msg = "managed worker received an unexpected broker response"
                raise DriverError(msg)
            offer: DecisionOffer = {**response["offer"], "instructions": instructions}
            response = self._submit_decision(offer, info)

    def _submit_decision(self, offer: DecisionOffer, info: GameInfo) -> PlayReply:
        schema = managed_proposal_schema(
            offer, memory_enabled=info["memory_enabled"], memory_max_chars=info["memory_max_chars"]
        )
        decision_info = {**info, "proposal_schema": schema}
        deadline = time.monotonic() + self.driver.profile.timeout_seconds
        arena_deadline = offer.get("deadline")
        if isinstance(arena_deadline, str):
            remaining = (datetime.fromisoformat(arena_deadline) - datetime.now(UTC)).total_seconds()
            deadline = min(deadline, time.monotonic() + remaining)
        request_offer: DecisionOffer = offer
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
                parsed = parse_managed_proposal(proposal)
                if parsed.explanation is not None and parsed.explanation.strip() != "":
                    self.driver.emit_trace({
                        "type": "decision_explanation",
                        **context,
                        "submission_id": parsed.submission_id,
                        "text": parsed.explanation.strip(),
                    })
                return self.session.play(self.session.session_id, parsed.game_proposal(), cancel=self.stopped)
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
                if isinstance(error, HarnessError) and error.code == "cancelled":
                    self.driver.emit_trace({"type": "delivery_cancelled", **context, "error": feedback})
                    raise
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


def _bounded_data(name: str, value: Mapping[str, object], max_bytes: int) -> dict[str, Any]:
    """Keep private diagnostics useful without unbounded prompts or trace records."""
    encoded = json.dumps(dict(value), ensure_ascii=True, separators=(",", ":"))
    if len(encoded) <= max_bytes:
        return {name: value}
    # A JSON excerpt is escaped again by the enclosing trace/prompt JSON.
    excerpt_bytes = min(max_bytes, 16_384)
    return {f"{name}_excerpt": encoded[:excerpt_bytes], f"{name}_truncated": True}
