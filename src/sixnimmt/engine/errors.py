"""Rejections raised by the pure engine. A rejection leaves state unchanged."""

from enum import StrEnum


class ErrorCode(StrEnum):
    MESSAGE_TOO_LONG = "message_too_long"
    DIRECT_MESSAGES_DISABLED = "direct_messages_disabled"
    RECIPIENT_NOT_FOUND = "recipient_not_found"
    MALFORMED_REQUEST = "malformed_request"
    UNKNOWN_PLAYER = "unknown_player"
    MATCH_FINISHED = "match_finished"
    WRONG_PHASE = "wrong_phase"
    NOT_YOUR_TURN = "not_your_turn"
    CARD_NOT_IN_HAND = "card_not_in_hand"
    NO_SELECTION_TO_COMMIT = "no_selection_to_commit"
    CANNOT_UNCOMMIT_WHEN_ALL_COMMITTED = "cannot_uncommit_when_all_committed"
    COMMUNICATION_DISABLED = "communication_disabled"
    ACTION_BUDGET_EXHAUSTED = "action_budget_exhausted"
    INVALID_ROW_INDEX = "invalid_row_index"
    INVALID_PLAYER_COUNT = "invalid_player_count"
    DUPLICATE_PLAYER_ID = "duplicate_player_id"
    INVALID_PLAYER_ID = "invalid_player_id"


class EngineRejection(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
