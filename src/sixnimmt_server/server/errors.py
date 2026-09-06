"""Error codes the API returns, and the mapping from engine rejections onto them.

Every message obeys §5.5: it may state only what the caller could already read
from their own view. Naming another player's card, hand, or message content in
an error would turn speculative actions into a probe for hidden state.
"""

from enum import StrEnum
from http import HTTPStatus

from sixnimmt_server.engine.errors import ErrorCode


class ApiErrorCode(StrEnum):
    """The wire codes. Engine rejections map onto a subset of these."""

    MATCH_NOT_FOUND = "MATCH_NOT_FOUND"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    MATCH_NOT_STARTED = "MATCH_NOT_STARTED"
    MATCH_ALREADY_STARTED = "MATCH_ALREADY_STARTED"
    MATCH_FINISHED = "MATCH_FINISHED"
    MATCH_ABANDONED = "MATCH_ABANDONED"
    WRONG_PHASE = "WRONG_PHASE"
    NOT_YOUR_TURN = "NOT_YOUR_TURN"
    CARD_NOT_IN_HAND = "CARD_NOT_IN_HAND"
    NO_SELECTION_TO_COMMIT = "NO_SELECTION_TO_COMMIT"
    CANNOT_UNCOMMIT_WHEN_ALL_COMMITTED = "CANNOT_UNCOMMIT_WHEN_ALL_COMMITTED"
    COMMUNICATION_DISABLED = "COMMUNICATION_DISABLED"
    INVALID_ROW_INDEX = "INVALID_ROW_INDEX"
    ROW_ALREADY_CHOSEN = "ROW_ALREADY_CHOSEN"
    ACTION_BUDGET_EXHAUSTED = "ACTION_BUDGET_EXHAUSTED"
    MESSAGE_TOO_LONG = "MESSAGE_TOO_LONG"
    DIRECT_MESSAGES_DISABLED = "DIRECT_MESSAGES_DISABLED"
    RECIPIENT_NOT_FOUND = "RECIPIENT_NOT_FOUND"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    MALFORMED_REQUEST = "MALFORMED_REQUEST"
    # Beyond §9.4's minimum set: a match whose durable log could not be written.
    # The request was well formed and the match still exists, so it is neither a
    # client error nor a conflict — the server cannot serve the match right now.
    MATCH_UNAVAILABLE = "MATCH_UNAVAILABLE"
    UNKNOWN_ACTION_TYPE = "UNKNOWN_ACTION_TYPE"
    UNKNOWN_PLAYER = "UNKNOWN_PLAYER"
    INVALID_PLAYER_COUNT = "INVALID_PLAYER_COUNT"
    DUPLICATE_PLAYER_ID = "DUPLICATE_PLAYER_ID"
    INVALID_PLAYER_ID = "INVALID_PLAYER_ID"


_ENGINE_TO_API: dict[ErrorCode, ApiErrorCode] = {
    ErrorCode.MESSAGE_TOO_LONG: ApiErrorCode.MESSAGE_TOO_LONG,
    ErrorCode.DIRECT_MESSAGES_DISABLED: ApiErrorCode.DIRECT_MESSAGES_DISABLED,
    ErrorCode.RECIPIENT_NOT_FOUND: ApiErrorCode.RECIPIENT_NOT_FOUND,
    ErrorCode.MALFORMED_REQUEST: ApiErrorCode.MALFORMED_REQUEST,
    ErrorCode.UNKNOWN_PLAYER: ApiErrorCode.UNKNOWN_PLAYER,
    ErrorCode.MATCH_FINISHED: ApiErrorCode.MATCH_FINISHED,
    ErrorCode.WRONG_PHASE: ApiErrorCode.WRONG_PHASE,
    ErrorCode.NOT_YOUR_TURN: ApiErrorCode.NOT_YOUR_TURN,
    ErrorCode.CARD_NOT_IN_HAND: ApiErrorCode.CARD_NOT_IN_HAND,
    ErrorCode.NO_SELECTION_TO_COMMIT: ApiErrorCode.NO_SELECTION_TO_COMMIT,
    ErrorCode.CANNOT_UNCOMMIT_WHEN_ALL_COMMITTED: ApiErrorCode.CANNOT_UNCOMMIT_WHEN_ALL_COMMITTED,
    ErrorCode.COMMUNICATION_DISABLED: ApiErrorCode.COMMUNICATION_DISABLED,
    ErrorCode.ACTION_BUDGET_EXHAUSTED: ApiErrorCode.ACTION_BUDGET_EXHAUSTED,
    ErrorCode.INVALID_ROW_INDEX: ApiErrorCode.INVALID_ROW_INDEX,
    ErrorCode.INVALID_PLAYER_COUNT: ApiErrorCode.INVALID_PLAYER_COUNT,
    ErrorCode.DUPLICATE_PLAYER_ID: ApiErrorCode.DUPLICATE_PLAYER_ID,
    ErrorCode.INVALID_PLAYER_ID: ApiErrorCode.INVALID_PLAYER_ID,
}

_STATUSES: dict[ApiErrorCode, HTTPStatus] = {
    ApiErrorCode.MATCH_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ApiErrorCode.NOT_AUTHORIZED: HTTPStatus.UNAUTHORIZED,
    ApiErrorCode.MALFORMED_REQUEST: HTTPStatus.BAD_REQUEST,
    ApiErrorCode.MATCH_UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
    ApiErrorCode.UNKNOWN_ACTION_TYPE: HTTPStatus.BAD_REQUEST,
    ApiErrorCode.INVALID_PLAYER_COUNT: HTTPStatus.BAD_REQUEST,
    ApiErrorCode.DUPLICATE_PLAYER_ID: HTTPStatus.BAD_REQUEST,
    ApiErrorCode.INVALID_PLAYER_ID: HTTPStatus.BAD_REQUEST,
}
# Everything else is a well-formed request that conflicts with the match state.
_DEFAULT_STATUS = HTTPStatus.CONFLICT


def api_code_for(code: ErrorCode) -> ApiErrorCode:
    """The wire code for an engine rejection.

    An unmapped engine code becomes WRONG_PHASE rather than escaping as a 500:
    a new rejection reaching a client should still be a well-formed refusal.
    """
    return _ENGINE_TO_API.get(code, ApiErrorCode.WRONG_PHASE)


def status_for(code: ApiErrorCode) -> HTTPStatus:
    return _STATUSES.get(code, _DEFAULT_STATUS)


class ApiError(Exception):
    """A refusal to be rendered as the §9.4 error body.

    `legal_actions` and `view_version` are filled in by the request handler,
    which knows the caller and can therefore read their own view safely.
    """

    def __init__(
        self,
        code: ApiErrorCode,
        message: str,
        legal_actions: tuple[str, ...] = (),
        view_version: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.legal_actions = legal_actions
        # The caller's own cursor, never a global one (§5.5). None where the
        # caller is not yet identified, as for an unparseable request.
        self.view_version = view_version

    @property
    def status(self) -> HTTPStatus:
        return status_for(self.code)


def match_not_found(match_id: str) -> ApiError:
    """The single refusal for an unreadable match.

    A token that names a match it has no rights to, and a match that does not
    exist, must be indistinguishable — same code, same shape, same path — or a
    token becomes a probe for which match IDs exist.
    """
    return ApiError(ApiErrorCode.MATCH_NOT_FOUND, f"no match {match_id} is available to this token")
