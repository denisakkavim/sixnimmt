"""GameRules, MatchProtocol, and InformationPolicy defaults and validation."""

import pytest
from pydantic import ValidationError

from sixnimmt.engine.rules import (
    CardSelectionPolicy,
    EndCondition,
    GameRules,
    InformationPolicy,
    MatchProtocol,
    OnInvalidAction,
    PrivateMessageExistence,
)


def test_game_rules_defaults_describe_the_published_game() -> None:
    rules = GameRules()

    assert rules.min_players == 2
    assert rules.max_players == 10
    assert rules.cards_per_hand == 10
    assert rules.row_count == 4
    assert rules.row_capacity == 5
    assert rules.deck_size == 104
    assert rules.target_score == 66


def test_information_policy_defaults_hide_cards_and_show_message_facts() -> None:
    policy = InformationPolicy()

    assert policy.card_selection == CardSelectionPolicy.HIDDEN
    assert policy.private_message_existence == PrivateMessageExistence.VISIBLE


def test_match_protocol_defaults_play_to_66_without_communication() -> None:
    protocol = MatchProtocol()

    assert protocol.end_condition == EndCondition.TARGET_SCORE
    assert protocol.hands is None
    assert protocol.communication_enabled is False
    assert protocol.information_policy == InformationPolicy()
    assert protocol.allow_direct_messages is True
    assert protocol.max_actions_per_play is None
    assert protocol.max_message_length == 2000
    assert protocol.on_invalid_action == OnInvalidAction.REJECT
    assert protocol.anonymise_display_names is False


@pytest.mark.parametrize(
    ("kwargs", "description"),
    [
        ({"min_players": 1}, "fewer than 2 players"),
        ({"max_players": 11}, "more than 10 players"),
        ({"min_players": 6, "max_players": 4}, "min above max"),
    ],
)
def test_game_rules_rejects_player_counts_outside_2_to_10(kwargs: dict, description: str) -> None:
    with pytest.raises(ValidationError):
        GameRules(**kwargs)


@pytest.mark.parametrize("hands", [None, 0, -1])
def test_fixed_hands_protocol_requires_a_positive_hand_count(hands: int | None) -> None:
    with pytest.raises(ValidationError):
        MatchProtocol(end_condition=EndCondition.FIXED_HANDS, hands=hands)


def test_rules_models_round_trip_through_json() -> None:
    protocol = MatchProtocol(end_condition=EndCondition.FIXED_HANDS, hands=4)

    restored = MatchProtocol.model_validate_json(protocol.model_dump_json())

    assert restored == protocol
    assert restored.hands == 4


def test_unknown_protocol_flags_are_rejected_instead_of_selecting_classic_mode() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        MatchProtocol.model_validate({"unknown_mode_enabled": True})


def test_rules_models_are_immutable() -> None:
    rules = GameRules()

    with pytest.raises(ValidationError):
        rules.target_score = 100  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("cards_per_hand", 1),
        ("row_count", 6),
        ("row_capacity", 3),
        ("deck_size", 60),
    ],
)
def test_game_rules_rejects_a_shape_the_engine_does_not_play(field_name: str, value: int) -> None:
    """Dealing, row layout and row closure are built to the published numbers.

    Accepting a different value would report a ruleset the match never plays.
    """
    with pytest.raises(ValidationError):
        GameRules(**{field_name: value})


def test_game_rules_allows_a_target_score_the_engine_honours() -> None:
    """Unlike the table's shape, the winning threshold is read from the rules."""
    rules = GameRules(target_score=30)

    assert rules.target_score == 30
