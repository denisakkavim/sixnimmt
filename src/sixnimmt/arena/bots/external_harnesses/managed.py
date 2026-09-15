"""Own fresh harness invocations within the arena bot lifecycle."""

import time
from datetime import UTC, datetime
from threading import Event, Thread
from typing import Any

from sixnimmt.arena.bots.external_harnesses.broker import PLAY_INSTRUCTIONS, SeatSession
from sixnimmt.arena.bots.external_harnesses.drivers import DriverError, DriverFormatError, ManagedCommandDriver
from sixnimmt.arena.bots.external_harnesses.protocol import PROPOSAL_SCHEMA, HarnessError
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
        deadline = time.monotonic() + self.driver.profile.timeout_seconds
        arena_deadline = offer.get("deadline")
        if isinstance(arena_deadline, str):
            remaining = (datetime.fromisoformat(arena_deadline) - datetime.now(UTC)).total_seconds()
            deadline = min(deadline, time.monotonic() + remaining)
        for attempt in range(2):
            try:
                proposal = self.driver.invoke(offer, info, PROPOSAL_SCHEMA, deadline=deadline)
                return self.session.play(self.session.session_id, proposal, cancel=self.stopped)
            except (DriverFormatError, HarnessError) as error:
                if attempt == 1:
                    raise
                if isinstance(error, HarnessError) and error.code not in (
                    "invalid_proposal",
                    "invalid_memory",
                    "unavailable_action",
                ):
                    raise
                offer = {
                    **offer,
                    "protocol_error": {
                        "code": error.code if isinstance(error, HarnessError) else "invalid_final_json",
                        "message": str(error),
                    },
                }
        msg = "managed proposal repair limit exhausted"
        raise DriverError(msg)
