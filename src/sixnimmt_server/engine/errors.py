"""Rejections raised by the pure engine. A rejection leaves state unchanged."""

from enum import StrEnum


class ErrorCode(StrEnum):
    UNKNOWN_PLAYER = "unknown_player"
    MATCH_FINISHED = "match_finished"
    WRONG_PHASE = "wrong_phase"
    CARD_NOT_IN_HAND = "card_not_in_hand"
    NO_SELECTION_TO_COMMIT = "no_selection_to_commit"
    NEGOTIATION_DISABLED = "negotiation_disabled"
    INVALID_ROW_INDEX = "invalid_row_index"
    ROW_ALREADY_CHOSEN = "row_already_chosen"
    NOT_AWAITING_PLAYER = "not_awaiting_player"


class EngineRejection(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
