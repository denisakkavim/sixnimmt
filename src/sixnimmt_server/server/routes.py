"""The HTTP surface: match management, reading state, and submitting actions."""

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import TypeAdapter, ValidationError

from sixnimmt_server.engine.actions import (
    Action,
    ActionType,
    ChooseRowAction,
    CommitAction,
    SelectCardAction,
    SendMessageAction,
    UncommitAction,
)
from sixnimmt_server.engine.audience import Viewer
from sixnimmt_server.engine.state import Phase, PlayerSeat
from sixnimmt_server.engine.views import MatchView
from sixnimmt_server.server.errors import ApiError, ApiErrorCode
from sixnimmt_server.server.schemas import (
    CreateMatchRequest,
    CreateMatchResponse,
    EventsResponse,
    MatchListResponse,
    MatchSummary,
    WaitResponse,
    serialise_event,
)
from sixnimmt_server.server.sink import SinkClosed
from sixnimmt_server.server.store import MatchRecord, MatchStore

DEFAULT_WAIT_SECONDS = 30.0
MAX_WAIT_SECONDS = 60.0
# How long a quiet stream goes before a comment reassures the client it is alive.
STREAM_KEEPALIVE_SECONDS = 15.0

_ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)

router = APIRouter()


def _store(request: Request) -> MatchStore:
    return request.app.state.store


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _require_admin(request: Request) -> None:
    if not request.app.state.tokens.is_admin(bearer_token(request)):
        raise ApiError(ApiErrorCode.NOT_AUTHORIZED, "this endpoint requires an admin token")


def _authorise(request: Request, match_id: str) -> tuple[MatchRecord, Viewer]:
    return _store(request).authorise(bearer_token(request), match_id)


def _with_caller_context(error: ApiError, record: MatchRecord, viewer: Viewer) -> ApiError:
    """Give an error the caller's own legal actions and cursor, if it has none.

    Both come from the caller's own view, so an error can never carry more than
    they could already read.
    """
    if error.view_version is not None:
        return error
    view = record.view_for(viewer)
    error.legal_actions = view.legal_actions
    error.view_version = view.view_version
    return error


def _parse_action(payload: dict[str, Any]) -> Action:
    action_type = payload.get("type")
    if action_type not in {member.value for member in ActionType}:
        raise ApiError(
            ApiErrorCode.UNKNOWN_ACTION_TYPE,
            f"unknown action type {action_type!r}; expected one of: "
            + ", ".join(member.value for member in ActionType),
        )
    try:
        return _ACTION_ADAPTER.validate_python(payload)
    except ValidationError as invalid:
        raise ApiError(
            ApiErrorCode.MALFORMED_REQUEST, f"the action could not be read: {invalid.error_count()} problems"
        ) from invalid


async def _submit(request: Request, match_id: str, action: Action) -> MatchView:
    record, viewer = _authorise(request, match_id)
    try:
        return await _store(request).apply_action(record, viewer, action)
    except ApiError as refusal:
        raise _with_caller_context(refusal, record, viewer) from None


@router.post("/matches", response_model=CreateMatchResponse)
async def create_match(request: Request, body: CreateMatchRequest) -> CreateMatchResponse:
    """Open a match and mint its tokens. Admin only; the seed is returned here alone."""
    _require_admin(request)
    seats = [
        PlayerSeat(player_id=player.id, display_name=player.display_name, agent_metadata=player.agent_metadata)
        for player in body.players
    ]
    record, tokens, seed = _store(request).create(seats, body.seed, body.rules, body.protocol)
    return CreateMatchResponse(
        match_id=record.match_id,
        rules=record.rules,
        protocol=record.protocol,
        seed=seed,
        player_tokens=tokens.player_tokens,
        public_spectator_token=tokens.public_spectator_token,
        omniscient_token=tokens.omniscient_token,
    )


@router.get("/matches", response_model=MatchListResponse)
async def list_matches(request: Request) -> MatchListResponse:
    """Every match with its status and scores. Admin only: it names match IDs."""
    _require_admin(request)
    store = _store(request)
    summaries = []
    for match_id in store.match_ids:
        record = store.record_for(match_id)
        if record is None:
            continue
        summaries.append(
            MatchSummary(
                match_id=record.match_id,
                status=record.status,
                hand_number=record.state.hand_number,
                play_number=record.state.play_number,
                scores={player.player_id: player.total_score for player in record.state.players},
            )
        )
    return MatchListResponse(matches=summaries)


@router.post("/matches/{match_id}/start", response_model=MatchView)
async def start_match(request: Request, match_id: str) -> MatchView:
    """Deal the first hand and open the first play."""
    _require_admin(request)
    record, viewer = _authorise(request, match_id)
    await _store(request).start(record)
    return record.view_for(viewer)


@router.delete("/matches/{match_id}", status_code=204)
async def abandon_match(request: Request, match_id: str) -> None:
    """Abandon a match: append the event, wake its subscribers, release resources."""
    _require_admin(request)
    record, _ = _authorise(request, match_id)
    await _store(request).abandon(record)


@router.get("/matches/{match_id}/state", response_model=MatchView)
async def read_state(request: Request, match_id: str) -> MatchView:
    """The caller's view, folded from the events they may see."""
    record, viewer = _authorise(request, match_id)
    return record.view_for(viewer)


@router.get("/matches/{match_id}/events", response_model=EventsResponse)
async def read_events(
    request: Request,
    match_id: str,
    since: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> EventsResponse:
    """The caller's own event stream, contiguous from 1, `since` exclusive."""
    record, viewer = _authorise(request, match_id)
    numbered = record.sink.events_since(viewer, since, limit)
    return EventsResponse(
        match_id=match_id,
        view_version=record.sink.view_version(viewer),
        events=[serialise_event(cursor, event, viewer.role) for cursor, event in numbered],
    )


@router.get("/matches/{match_id}/wait", response_model=WaitResponse)
async def wait_for_events(
    request: Request,
    match_id: str,
    since: Annotated[int, Query(ge=0)] = 0,
    timeout: Annotated[float, Query(gt=0)] = DEFAULT_WAIT_SECONDS,
) -> WaitResponse:
    """Long poll until this caller's own stream advances, or they can act.

    A transport convenience with no effect on the game. It wakes only on the
    caller's visible stream, so an event they may not see neither returns this
    call early nor moves their cursor.
    """
    record, viewer = _authorise(request, match_id)
    subscription = record.sink.subscribe(viewer)
    deadline = min(timeout, MAX_WAIT_SECONDS)
    try:
        # Subscribed before the first check, so an event landing between the two
        # is delivered to the subscription rather than missed.
        while True:
            if record.abandoned:
                # §11.4: an outstanding poll ends here rather than hanging on a
                # match that will never produce another event.
                raise ApiError(ApiErrorCode.MATCH_ABANDONED, f"match {match_id} has been abandoned")
            if _should_return_now(record, viewer, since):
                return _wait_response(record, viewer, since, timed_out=False)
            if await subscription.next_event(deadline) is None:
                return _wait_response(record, viewer, since, timed_out=True)
    except SinkClosed:
        raise ApiError(ApiErrorCode.MATCH_ABANDONED, f"match {match_id} has been abandoned") from None
    finally:
        record.sink.unsubscribe(subscription)


def _should_return_now(record: MatchRecord, viewer: Viewer, since: int) -> bool:
    if record.sink.events_since(viewer, since):
        return True
    view = record.view_for(viewer)
    if view.status != "in_progress":
        return True
    return _match_awaits(view, viewer)


def _match_awaits(view: MatchView, viewer: Viewer) -> bool:
    """Whether the match is blocked on this caller.

    Narrower than "has legal actions": classic mode keeps offering select_card
    to a player who has already committed, and treating that as a reason to
    return would make the long poll return instantly and always. What an agent
    wants to know is whether the game is waiting for them.
    """
    if not view.legal_actions:
        return False
    if view.phase == Phase.AWAITING_ROW_CHOICE:
        return view.awaiting == viewer.player_id
    if view.phase == Phase.SELECTING:
        return not view.you.committed
    return False


def _wait_response(record: MatchRecord, viewer: Viewer, since: int, timed_out: bool) -> WaitResponse:
    numbered = record.sink.events_since(viewer, since)
    view = record.view_for(viewer)
    return WaitResponse(
        match_id=record.match_id,
        view_version=view.view_version,
        events=[serialise_event(cursor, event, viewer.role) for cursor, event in numbered],
        legal_actions=list(view.legal_actions),
        timed_out=timed_out,
    )


@router.get("/matches/{match_id}/stream")
async def stream_events(
    request: Request,
    match_id: str,
    since: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    """Server-Sent Events, filtered to the caller and numbered by their cursor."""
    record, viewer = _authorise(request, match_id)
    last_event_id = request.headers.get("last-event-id")
    if last_event_id is not None and last_event_id.isdigit():
        since = int(last_event_id)
    return StreamingResponse(
        _stream_body(record, viewer, since),
        media_type="text/event-stream",
        headers={"cache-control": "no-store"},
    )


async def _stream_body(record: MatchRecord, viewer: Viewer, since: int) -> AsyncIterator[str]:
    """Replay from the caller's cursor, then follow their filtered stream live."""
    subscription = record.sink.subscribe(viewer)
    # Fixed here, before any delivery moves it: everything up to this point is
    # replayed, everything past it arrives through the subscription, and nothing
    # is sent twice.
    replay_through = subscription.cursor
    try:
        for cursor, event in record.sink.events_since(viewer, since):
            if cursor <= replay_through:
                yield _sse_message(cursor, serialise_event(cursor, event, viewer.role), event.type.value)
        while True:
            try:
                delivered = await subscription.next_event(STREAM_KEEPALIVE_SECONDS)
            except SinkClosed:
                # No id: this is the server explaining why the stream ended, not
                # a match event, and it must not look like a cursor position.
                yield _sse_message(None, {"code": ApiErrorCode.MATCH_ABANDONED.value}, "match_closed")
                return
            if delivered is None:
                # A comment keeps the connection alive without inventing an
                # event, which would advance a cursor nothing produced.
                yield ": keep-alive\n\n"
                continue
            cursor, event = delivered
            yield _sse_message(cursor, serialise_event(cursor, event, viewer.role), event.type.value)
    finally:
        record.sink.unsubscribe(subscription)


def _sse_message(cursor: int | None, payload: dict[str, Any], event_type: str) -> str:
    identifier = f"id: {cursor}\n" if cursor is not None else ""
    return f"{identifier}event: {event_type}\ndata: {json.dumps(payload)}\n\n"


@router.post("/matches/{match_id}/actions", response_model=MatchView)
async def submit_action(request: Request, match_id: str, body: Annotated[dict[str, Any], Body()]) -> MatchView:
    """The canonical action endpoint. Returns the caller's updated view."""
    return await _submit(request, match_id, _parse_action(body))


@router.post("/matches/{match_id}/select", response_model=MatchView)
async def select_card(request: Request, match_id: str, body: SelectCardAction) -> MatchView:
    return await _submit(request, match_id, body)


@router.post("/matches/{match_id}/commit", response_model=MatchView)
async def commit(request: Request, match_id: str, body: CommitAction) -> MatchView:
    return await _submit(request, match_id, body)


@router.post("/matches/{match_id}/uncommit", response_model=MatchView)
async def uncommit(request: Request, match_id: str, body: UncommitAction) -> MatchView:
    return await _submit(request, match_id, body)


@router.post("/matches/{match_id}/message", response_model=MatchView)
async def send_message(request: Request, match_id: str, body: SendMessageAction) -> MatchView:
    return await _submit(request, match_id, body)


@router.post("/matches/{match_id}/choose_row", response_model=MatchView)
async def choose_row(request: Request, match_id: str, body: ChooseRowAction) -> MatchView:
    return await _submit(request, match_id, body)


@router.get("/schemas")
async def json_schemas() -> dict[str, Any]:
    """JSON Schema for every action and event, so clients generate their types."""
    return {
        "action": _ACTION_ADAPTER.json_schema(),
        "view": MatchView.model_json_schema(),
    }
