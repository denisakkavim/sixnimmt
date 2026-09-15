"""The harness accepts only an explicit, complete transaction envelope."""

from typing import Any

import pytest

from sixnimmt.arena.bots.external_harnesses.protocol import MAX_PROPOSAL_BYTES, HarnessError, parse_proposal


@pytest.fixture
def proposal() -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "session_id": "session",
        "decision_id": "decision",
        "submission_id": "submission",
        "view_id": "view",
        "actions": [{"type": "select_card", "card": 7}],
        "memory": None,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocol_version", True),
        ("protocol_version", 1.0),
        ("session_id", ""),
        ("session_id", "private/seat"),
        ("unknown", "field"),
        ("memory", 12),
        ("memory", "\ud800"),
        ("actions", [{"type": "select_card", "card": "7"}]),
        ("actions", [{"type": "select_card", "card": True}]),
        ("actions", [{"type": "select_card", "card": 7, "from_view": "other"}]),
        ("actions", [{"type": "send_message", "visibility": "direct", "body": "hi"}]),
        ("actions", [{"type": "commit", "extra": None}]),
        ("actions", []),
    ],
)
def test_rejects_inexact_proposal_fields(proposal: dict[str, Any], field: str, value: Any) -> None:
    proposal[field] = value
    with pytest.raises(HarnessError, match="Proposal"):
        parse_proposal(proposal)


def test_requires_explicit_memory_field(proposal: dict[str, Any]) -> None:
    del proposal["memory"]
    with pytest.raises(HarnessError, match="memory"):
        parse_proposal(proposal)


def test_memory_only_proposal_counts_as_one_operation(proposal: dict[str, Any]) -> None:
    proposal.update(actions=[], memory="Remember the table message.")
    batch = parse_proposal(proposal).batch()
    assert batch.actions == ()
    assert batch.size == 1


def test_memory_update_counts_against_eight_operation_limit(proposal: dict[str, Any]) -> None:
    proposal.update(actions=[{"type": "commit"}] * 8, memory="memory")
    with pytest.raises(HarnessError):
        parse_proposal(proposal)


def test_server_assigns_action_metadata(proposal: dict[str, Any]) -> None:
    batch = parse_proposal(proposal).batch()
    assert batch.actions[0].from_view == "view"
    assert batch.actions[0].action_id is not None
    assert batch.actions[0].expected_view_version is None


def test_bounds_proposal_bytes_before_model_validation(proposal: dict[str, Any]) -> None:
    proposal["memory"] = "x" * MAX_PROPOSAL_BYTES
    with pytest.raises(HarnessError, match="exceeds"):
        parse_proposal(proposal)
