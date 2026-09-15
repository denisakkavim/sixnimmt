"""The local seat transport enforces authentication, framing, and cancellation."""

import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

from sixnimmt.arena.bots.external_harnesses.messages import (
    GAME_INFO_ADAPTER,
    PLAY_REPLY_ADAPTER,
    DecisionReply,
    GameInfo,
)
from sixnimmt.arena.bots.external_harnesses.protocol import HarnessError
from sixnimmt.arena.bots.external_harnesses.transport import MAX_FRAME_BYTES, ControllerServer, SeatClient


class BlockingSeat:
    def __init__(self, session_id: str = "seat-a", credential: str = "credential-a") -> None:
        self.session_id = session_id
        self.credential = credential
        self.started = threading.Event()
        self.released = threading.Event()
        self.cancelled = threading.Event()
        self.proposals: list[dict[str, Any] | None] = []

    def get_game_info(self, session_id: str) -> GameInfo:
        return {
            "protocol_version": 1,
            "session_id": session_id,
            "player_id": self.session_id,
            "name": self.session_id,
            "rules": {},
            "protocol": {},
            "instructions": "Choose an action.",
            "proposal_schema": {},
            "memory_enabled": False,
            "memory_max_chars": 4000,
        }

    def play(
        self, session_id: str, proposal: dict[str, Any] | None = None, cancel: threading.Event | None = None
    ) -> DecisionReply:
        self.proposals.append(proposal)
        self.started.set()
        while not self.released.wait(0.01):
            if cancel is not None and cancel.is_set():
                self.cancelled.set()
                raise HarnessError("cancelled", "The wait was cancelled.")
        return {
            "status": "decision",
            "receipt": None,
            "result": None,
            "offer": {
                "protocol_version": 1,
                "session_id": session_id,
                "decision_id": "decision-1",
                "view_id": "view-1",
                "view": {},
                "observation": "",
                "instructions": "",
                "action_tools": [],
                "rejection": None,
                "memory": None,
                "deadline": None,
            },
        }


def send_raw(endpoint: str, payload: bytes) -> dict[str, Any]:
    port = urlsplit(endpoint).port
    assert port is not None
    with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
        connection.sendall(payload)
        with connection.makefile("rb") as response:
            return json.loads(response.readline())


def test_returns_only_the_authenticated_seat_information() -> None:
    seats = [BlockingSeat(), BlockingSeat("seat-b", "credential-b")]
    with ControllerServer(seats) as controller:
        client = SeatClient(controller.endpoint, "credential-a")
        info = client.get_game_info("seat-a")
        assert info["session_id"] == "seat-a"
        assert info["player_id"] == "seat-a"
        assert "seat-b" not in json.dumps(info)


@pytest.mark.parametrize(
    ("session_id", "credential"),
    [("seat-a", "incorrect"), ("seat-b", "credential-a"), ("unknown", "credential-a")],
)
def test_rejects_wrong_credentials_and_cross_seat_access(session_id: str, credential: str) -> None:
    with (
        ControllerServer([BlockingSeat(), BlockingSeat("seat-b", "credential-b")]) as controller,
        pytest.raises(HarnessError) as error,
    ):
        SeatClient(controller.endpoint, credential).get_game_info(session_id)
    assert error.value.code == "unauthorized"
    assert credential not in error.value.message


def test_holds_play_until_a_decision_is_available() -> None:
    seat = BlockingSeat()
    proposal = {"submission_id": "retry-same-id"}
    with ControllerServer([seat]) as controller, ThreadPoolExecutor() as worker:
        client = SeatClient(controller.endpoint, seat.credential)
        pending = worker.submit(client.play, seat.session_id, proposal)
        assert seat.started.wait(2)
        assert not pending.done()
        seat.released.set()
        response = pending.result(timeout=2)
        assert response["status"] == "decision"
        assert response["offer"]["session_id"] == seat.session_id
    assert seat.proposals == [proposal]


def test_client_cancellation_closes_the_connection_and_releases_the_broker_wait() -> None:
    seat = BlockingSeat()
    cancel = threading.Event()
    with ControllerServer([seat]) as controller, ThreadPoolExecutor() as worker:
        client = SeatClient(controller.endpoint, seat.credential)
        pending = worker.submit(client.play, seat.session_id, None, cancel)
        assert seat.started.wait(2)
        cancel.set()
        with pytest.raises(HarnessError) as error:
            pending.result(timeout=2)
        assert error.value.code == "cancelled"
        assert seat.cancelled.wait(2)
        assert client.get_game_info(seat.session_id)["session_id"] == seat.session_id


def test_controller_shutdown_releases_pending_requests() -> None:
    seat = BlockingSeat()
    controller = ControllerServer([seat]).start()
    with ThreadPoolExecutor() as worker:
        pending = worker.submit(SeatClient(controller.endpoint, seat.credential).play, seat.session_id)
        try:
            assert seat.started.wait(2)
        finally:
            controller.close()
        assert seat.cancelled.wait(2)
        with pytest.raises(HarnessError) as error:
            pending.result(timeout=2)
        assert error.value.code == "disconnected"
    controller.close()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"method":"play","method":"get_game_info"}\n',
        b'{"proposal":{"memory":NaN}}\n',
        b'{"proposal":{"number":1e999}}\n',
        b"[]\n",
        b"{}\n{}\n",
    ],
)
def test_rejects_ambiguous_or_invalid_json_frames(payload: bytes) -> None:
    with ControllerServer([BlockingSeat()]) as controller:
        response = send_raw(controller.endpoint, payload)
    assert response["error"]["code"] == "invalid_request"


def test_rejects_oversized_request_before_broker_dispatch() -> None:
    seat = BlockingSeat()
    with ControllerServer([seat]) as controller:
        response = send_raw(controller.endpoint, b" " * MAX_FRAME_BYTES + b"\n")
    assert response["error"]["code"] == "message_too_large"
    assert not seat.started.is_set()


@pytest.mark.parametrize("endpoint", ["tcp://example.com:123", "http://127.0.0.1:123", "tcp://127.0.0.1:123/path"])
def test_rejects_nonlocal_or_malformed_controller_endpoints(endpoint: str) -> None:
    with pytest.raises(HarnessError) as error:
        SeatClient(endpoint, "credential")
    assert error.value.code == "invalid_config"


def test_rejects_unknown_authenticated_operations() -> None:
    request = {"session_id": "seat-a", "credential": "credential-a", "method": "transition"}
    with ControllerServer([BlockingSeat()]) as controller:
        response = send_raw(controller.endpoint, json.dumps(request).encode() + b"\n")
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("method", ["get_game_info", "play"])
def test_client_rejects_malformed_response_before_exposing_it(monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    client = SeatClient("tcp://127.0.0.1:123", "credential")
    # Replace only the external wire response, retaining client validation.
    monkeypatch.setattr(client, "_request", lambda *args: {"status": "decision", "offer": None})
    with pytest.raises(HarnessError) as error:
        if method == "get_game_info":
            client.get_game_info("seat-a")
        else:
            client.play("seat-a")
    assert error.value.code == "invalid_response"


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
@pytest.mark.parametrize("response_type", ["game_info", "decision"])
def test_response_protocol_version_rejects_noninteger_or_unsupported_values(
    version: object, response_type: str
) -> None:
    seat = BlockingSeat()
    seat.released.set()
    if response_type == "game_info":
        info: dict[str, Any] = dict(seat.get_game_info(seat.session_id))
        info["protocol_version"] = version
        with pytest.raises(ValidationError):
            GAME_INFO_ADAPTER.validate_python(info, strict=True)
    else:
        response: dict[str, Any] = dict(seat.play(seat.session_id))
        response["offer"]["protocol_version"] = version
        with pytest.raises(ValidationError):
            PLAY_REPLY_ADAPTER.validate_python(response, strict=True)
