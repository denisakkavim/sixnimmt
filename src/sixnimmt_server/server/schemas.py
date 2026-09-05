"""Request and response bodies for the HTTP API.

Event serialisation is the one place a global counter could reach a player, so
it is stripped here by role rather than trusted to callers (§10.2).
"""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sixnimmt_server.common.text import check_representable
from sixnimmt_server.engine.events import Event
from sixnimmt_server.engine.rules import GameRules, MatchProtocol
from sixnimmt_server.engine.views import ViewRole

# Only these roles may see the match's global sequence. A player sees their own
# gap-free cursor and nothing that counts events they were never sent.
_ROLES_SEEING_GLOBAL_SEQUENCE = (ViewRole.ADMIN, ViewRole.OMNISCIENT_OBSERVER)


# A display name and a seat's metadata are copied into events, written to the
# log and rendered back into responses, so both have to survive a UTF-8
# encoder. JSON can carry a lone surrogate that UTF-8 cannot, which would
# otherwise be accepted at creation and fail every read of the match afterwards.
MAX_DISPLAY_NAME_LENGTH = 200
MAX_METADATA_BYTES = 4096


class PlayerSpec(BaseModel):
    """One seat as supplied at creation. Seating order is the deal order."""

    model_config = ConfigDict(frozen=True)

    id: str
    display_name: str = Field(default="", max_length=MAX_DISPLAY_NAME_LENGTH)
    agent_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("display_name")
    @classmethod
    def _check_display_name(cls, value: str) -> str:
        check_representable(value)
        return value

    @field_validator("agent_metadata")
    @classmethod
    def _check_agent_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        check_representable(value)
        # Opaque to the server but not unbounded: it is written to the log on
        # every match, and nothing downstream benefits from an unlimited blob.
        encoded = json.dumps(value).encode("utf-8")
        if len(encoded) > MAX_METADATA_BYTES:
            msg = f"is larger than the {MAX_METADATA_BYTES} bytes a seat's metadata may occupy"
            raise ValueError(msg)
        return value


class CreateMatchRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    players: list[PlayerSpec]
    seed: int | None = None
    rules: GameRules | None = None
    protocol: MatchProtocol | None = None


class CreateMatchResponse(BaseModel):
    """Returned once, to the creator. The only place the seed ever appears."""

    model_config = ConfigDict(frozen=True)

    match_id: str
    rules: GameRules
    protocol: MatchProtocol
    seed: int
    player_tokens: dict[str, str]
    public_spectator_token: str
    omniscient_token: str


class MatchSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    match_id: str
    status: str
    hand_number: int
    play_number: int
    scores: dict[str, int]


class MatchListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    matches: list[MatchSummary]


class EventsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    match_id: str
    view_version: int
    events: list[dict[str, Any]]


class WaitResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    match_id: str
    view_version: int
    events: list[dict[str, Any]]
    legal_actions: list[str]
    timed_out: bool


class ErrorBody(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    legal_actions: list[str] = Field(default_factory=list)
    # The caller's own cursor. Never a global counter, even in an error (§5.5).
    view_version: int | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    error: ErrorBody


def serialise_event(cursor: int, event: Event, role: ViewRole) -> dict[str, Any]:
    """One event as this role may see it, numbered by their own cursor."""
    payload = event.model_dump(mode="json")
    if role not in _ROLES_SEEING_GLOBAL_SEQUENCE:
        payload.pop("seq", None)
        payload.pop("server_action_seq", None)
    payload["view_version"] = cursor
    return payload
