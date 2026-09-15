"""Typed broker messages shared by local sessions, managed workers, and clients."""

from typing import Annotated, Any, Literal, NotRequired, TypedDict

from pydantic import BeforeValidator, Field, JsonValue, TypeAdapter

from sixnimmt.arena.bots.agent_contract import ActionTool


def _check_protocol_version(value: object) -> object:
    if type(value) is not int:
        msg = "protocol_version must be an integer"
        raise ValueError(msg)
    return value


ProtocolVersion = Annotated[Literal[1], BeforeValidator(_check_protocol_version)]


class RejectionData(TypedDict):
    code: str
    message: str
    legal_actions: list[str]
    action: dict[str, JsonValue] | None


class GameInfo(TypedDict):
    protocol_version: ProtocolVersion
    session_id: str
    player_id: str
    name: str
    rules: dict[str, JsonValue]
    protocol: dict[str, JsonValue]
    instructions: str
    proposal_schema: dict[str, Any]
    memory_enabled: bool
    memory_max_chars: int


class DecisionOffer(TypedDict):
    protocol_version: ProtocolVersion
    session_id: str
    decision_id: str
    view_id: str
    view: dict[str, JsonValue]
    observation: str
    instructions: str
    action_tools: list[ActionTool]
    rejection: RejectionData | None
    memory: str | None
    deadline: str | None
    protocol_error: NotRequired[dict[str, Any]]


class SubmissionReceipt(TypedDict):
    submission_id: str
    decision_id: str
    view_id: str
    status: Literal["staged", "accepted", "rejected", "failed"]
    claimed: bool
    rejection: RejectionData | None
    reason: str | None


class TerminalResult(TypedDict):
    outcome: Literal["finished", "forfeited", "failed", "abandoned"]
    winners: list[str]
    scores: dict[str, int]
    reason: str | None
    final_view: dict[str, JsonValue] | None


class DecisionReply(TypedDict):
    status: Literal["decision"]
    offer: DecisionOffer
    receipt: SubmissionReceipt | None
    result: None


class TerminalReply(TypedDict):
    status: Literal["terminal"]
    offer: None
    receipt: SubmissionReceipt | None
    result: TerminalResult


class WaitingReply(TypedDict):
    status: Literal["already_waiting", "wait_expired"]
    offer: None
    receipt: SubmissionReceipt | None
    result: None


ReplyStatus = Literal["decision", "terminal", "already_waiting", "wait_expired"]
PlayReply = Annotated[DecisionReply | TerminalReply | WaitingReply, Field(discriminator="status")]

GAME_INFO_ADAPTER: TypeAdapter[GameInfo] = TypeAdapter(GameInfo)
PLAY_REPLY_ADAPTER: TypeAdapter[PlayReply] = TypeAdapter(PlayReply)
