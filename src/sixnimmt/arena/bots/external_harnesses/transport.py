"""Authenticated, bounded local transport between seat bridges and the arena."""

from __future__ import annotations

import hmac
import json
import math
import select
import socket
import socketserver
import threading
from collections.abc import Sequence
from contextlib import suppress
from types import TracebackType
from typing import Any, Never, Protocol, Self
from urllib.parse import urlsplit

from pydantic import ValidationError

from sixnimmt.arena.bots.external_harnesses.messages import GAME_INFO_ADAPTER, PLAY_REPLY_ADAPTER, GameInfo, PlayReply
from sixnimmt.arena.bots.external_harnesses.protocol import HarnessError

MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_CONNECTIONS = 64
CONNECT_TIMEOUT_SECONDS = 5.0
POLL_SECONDS = 0.1


class SeatEndpoint(Protocol):
    """The broker operations exposed by an authenticated local seat."""

    session_id: str
    credential: str

    def get_game_info(self, session_id: str) -> GameInfo: ...

    def play(
        self, session_id: str, proposal: dict[str, Any] | None = None, cancel: threading.Event | None = None
    ) -> PlayReply: ...


def _encode_frame(value: dict[str, Any]) -> bytes:
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8") + b"\n"
    if len(encoded) > MAX_FRAME_BYTES:
        raise HarnessError("message_too_large", "The harness message exceeds the local transport limit.")
    return encoded


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HarnessError("invalid_request", "Duplicate JSON object fields are not allowed.")
        result[key] = value
    return result


def _reject_constant(value: str) -> Never:
    raise HarnessError("invalid_request", "Non-finite JSON numbers are not allowed.")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise HarnessError("invalid_request", "Non-finite JSON numbers are not allowed.")
    return number


def decode_json(data: bytes) -> object:
    """Decode strict JSON without accepting duplicate keys or NaN."""
    try:
        return json.loads(
            data, object_pairs_hook=_unique_object, parse_constant=_reject_constant, parse_float=_finite_float
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HarnessError("invalid_request", "Expected valid JSON.") from exc


def decode_message(data: bytes) -> dict[str, Any]:
    """Decode one local transport message."""
    value = decode_json(data)
    if not isinstance(value, dict):
        raise HarnessError("invalid_request", "Expected a JSON object.")
    return value


def _read_frame(connection: socket.socket, cancel: threading.Event | None = None) -> dict[str, Any]:
    data = bytearray()
    while True:
        if cancel is not None and cancel.is_set():
            raise HarnessError("cancelled", "The harness request was cancelled.")
        try:
            chunk = connection.recv(min(65536, MAX_FRAME_BYTES + 1 - len(data)))
        except TimeoutError:
            if cancel is None:
                raise
            continue
        if len(chunk) == 0:
            raise HarnessError("disconnected", "The local harness connection closed.")
        data.extend(chunk)
        if len(data) > MAX_FRAME_BYTES:
            raise HarnessError("message_too_large", "The harness message exceeds the local transport limit.")
        if b"\n" in chunk:
            break
    if data[-1:] != b"\n" or data.count(b"\n") != 1:
        raise HarnessError("invalid_request", "Expected exactly one local transport message.")
    return decode_message(bytes(data))


def _error_response(error: HarnessError) -> dict[str, Any]:
    return {"error": {"code": error.code, "message": error.message}}


class _ControllerTCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False

    def __init__(self, sessions: Sequence[SeatEndpoint]) -> None:
        self.sessions = {session.session_id: session for session in sessions}
        if len(self.sessions) != len(sessions):
            raise HarnessError("invalid_config", "Seat session IDs must be unique.")
        self.stopping = threading.Event()
        self.capacity = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.connections: set[socket.socket] = set()
        self.connections_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _RequestHandler)

    def verify_request(self, request: Any, client_address: Any) -> bool:
        return not self.stopping.is_set() and self.capacity.acquire(blocking=False)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        with self.connections_lock:
            self.connections.add(request)
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.connections_lock:
                self.connections.discard(request)
            self.capacity.release()

    def dispatch(self, request: dict[str, Any], cancel: threading.Event) -> GameInfo | PlayReply:
        session_id = request.get("session_id")
        credential = request.get("credential")
        if not isinstance(session_id, str) or not isinstance(credential, str):
            raise HarnessError("unauthorized", "The seat credentials are invalid.")
        session = self.sessions.get(session_id)
        if session is None or not hmac.compare_digest(credential.encode(), session.credential.encode()):
            raise HarnessError("unauthorized", "The seat credentials are invalid.")
        method = request.get("method")
        allowed = {"session_id", "credential", "method"}
        if method == "play":
            allowed.add("proposal")
        if len(request.keys() - allowed) > 0:
            raise HarnessError("invalid_request", "The request contains unsupported fields.")
        if method == "get_game_info":
            return session.get_game_info(session_id)
        if method != "play":
            raise HarnessError("invalid_request", "Unknown harness operation.")
        proposal = request.get("proposal")
        if proposal is not None and not isinstance(proposal, dict):
            raise HarnessError("invalid_request", "The proposal must be an object or null.")
        return session.play(session_id, proposal, cancel)

    def close_connections(self) -> None:
        self.stopping.set()
        with self.connections_lock:
            for connection in self.connections:
                with suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)


class _RequestHandler(socketserver.BaseRequestHandler):
    server: _ControllerTCPServer
    request: socket.socket

    def handle(self) -> None:
        if self.server.stopping.is_set():
            return
        cancel = threading.Event()
        completed = threading.Event()
        monitor: threading.Thread | None = None
        try:
            self.request.settimeout(CONNECT_TIMEOUT_SECONDS)
            request = _read_frame(self.request)
            monitor = threading.Thread(target=self._monitor_disconnect, args=(cancel, completed))
            monitor.start()
            response = {"result": self.server.dispatch(request, cancel)}
        except HarnessError as exc:
            response = _error_response(exc)
        except (OSError, ValueError):
            response = _error_response(HarnessError("disconnected", "The local harness connection failed."))
        except Exception:
            # A broker failure must not expose arena state or credentials on the wire.
            response = _error_response(HarnessError("internal_error", "The controller could not complete the request."))
        finally:
            completed.set()
        try:
            if not cancel.is_set():
                self._send_response(response)
        finally:
            if monitor is not None:
                monitor.join()

    def _send_response(self, response: dict[str, Any]) -> None:
        try:
            self.request.sendall(_encode_frame(response))
        except HarnessError as exc:
            with suppress(OSError):
                self.request.sendall(_encode_frame(_error_response(exc)))
        except OSError:
            return

    def _monitor_disconnect(self, cancel: threading.Event, completed: threading.Event) -> None:
        while not completed.is_set():
            if self.server.stopping.is_set():
                cancel.set()
                return
            try:
                readable, _, _ = select.select([self.request], [], [], POLL_SECONDS)
            except (OSError, ValueError):
                cancel.set()
                return
            if len(readable) > 0:
                # This connection permits one request. EOF or further input both
                # terminate its wait; a reconnect can repeat an idempotent proposal.
                cancel.set()
                return


class ControllerServer:
    """Serve seat sessions on an ephemeral loopback port until closed."""

    def __init__(self, sessions: Sequence[SeatEndpoint]) -> None:
        self._server = _ControllerTCPServer(sessions)
        self.endpoint = f"tcp://127.0.0.1:{self._server.server_address[1]}"
        self._thread: threading.Thread | None = None
        self._closed = False

    def start(self) -> Self:
        if self._closed:
            raise HarnessError("closed", "The controller transport is closed.")
        if self._thread is None:
            self._thread = threading.Thread(target=self._serve, name="sixnimmt-controller")
            self._thread.start()
        return self

    def _serve(self) -> None:
        self._server.serve_forever(poll_interval=POLL_SECONDS)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.close_connections()
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join()
        self._server.server_close()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()


class SeatClient:
    """One authenticated request per local connection; never infer seat state from it."""

    def __init__(self, endpoint: str, credential: str) -> None:
        parsed = urlsplit(endpoint)
        try:
            port = parsed.port
        except ValueError as exc:
            raise HarnessError("invalid_config", "Invalid controller endpoint.") from exc
        if (
            parsed.scheme != "tcp"
            or parsed.hostname != "127.0.0.1"
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != ""
            or parsed.query != ""
            or parsed.fragment != ""
        ):
            raise HarnessError("invalid_config", "The controller endpoint must be an IPv4 loopback TCP address.")
        if credential == "":
            raise HarnessError("invalid_config", "A seat credential is required.")
        self._address = ("127.0.0.1", port)
        self._credential = credential

    def get_game_info(self, session_id: str, cancel: threading.Event | None = None) -> GameInfo:
        response = self._request("get_game_info", session_id, None, cancel)
        try:
            return GAME_INFO_ADAPTER.validate_python(response, strict=True)
        except ValidationError as error:
            raise HarnessError("invalid_response", "The controller returned invalid game information.") from error

    def play(
        self, session_id: str, proposal: dict[str, Any] | None = None, cancel: threading.Event | None = None
    ) -> PlayReply:
        response = self._request("play", session_id, proposal, cancel)
        try:
            return PLAY_REPLY_ADAPTER.validate_python(response, strict=True)
        except ValidationError as error:
            raise HarnessError("invalid_response", "The controller returned an invalid play response.") from error

    def _request(
        self, method: str, session_id: str, proposal: dict[str, Any] | None, cancel: threading.Event | None
    ) -> dict[str, Any]:
        cancellation = cancel if cancel is not None else threading.Event()
        if cancellation.is_set():
            raise HarnessError("cancelled", "The harness request was cancelled.")
        request: dict[str, Any] = {"method": method, "session_id": session_id, "credential": self._credential}
        if method == "play":
            request["proposal"] = proposal
        frame = _encode_frame(request)
        try:
            with socket.create_connection(self._address, timeout=CONNECT_TIMEOUT_SECONDS) as connection:
                connection.sendall(frame)
                connection.settimeout(POLL_SECONDS)
                response = _read_frame(connection, cancellation)
        except OSError as exc:
            raise HarnessError("disconnected", "Cannot reach the local table controller.") from exc
        error = response.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str) and isinstance(error.get("message"), str):
            raise HarnessError(error["code"], error["message"])
        result = response.get("result")
        if not isinstance(result, dict):
            raise HarnessError("invalid_response", "The controller returned an invalid response.")
        return result
