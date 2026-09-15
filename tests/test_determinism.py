"""Reproducibility: same seeds and choices always give the same match."""

import os
import subprocess
import sys
from pathlib import Path

from pydantic import TypeAdapter

from sixnimmt.arena.artifacts import load_run
from sixnimmt.engine.actions import Action
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.setup import create_match, start_hand
from sixnimmt.engine.state import MatchState, Phase
from sixnimmt.engine.transition import transition

_ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)


def _choose_row(state: MatchState) -> MatchState:
    assert state.phase == Phase.AWAITING_ROW_CHOICE
    assert state.resolution is not None and state.resolution.awaiting_player is not None
    action = _ACTION_ADAPTER.validate_python({"type": "choose_row", "row_index": 0})
    new_state, _ = transition(state, state.resolution.awaiting_player, action, MatchProtocol(), GameRules())
    return new_state


def _run_match(match_seed: int, player_ids: list[str], row_choice: int = 0) -> MatchState:
    state, _ = create_match("m_01", player_ids, match_seed=match_seed)
    while state.phase != Phase.FINISHED:
        for player in state.players:
            if state.phase == Phase.FINISHED:
                break
            card = player.hand[0]
            action = _ACTION_ADAPTER.validate_python({"type": "select_card", "card": card})
            state, _ = transition(state, player.player_id, action, MatchProtocol(), GameRules())
            while state.phase == Phase.AWAITING_ROW_CHOICE:
                assert state.resolution is not None and state.resolution.awaiting_player is not None
                choice = _ACTION_ADAPTER.validate_python({"type": "choose_row", "row_index": row_choice})
                state, _ = transition(state, state.resolution.awaiting_player, choice, MatchProtocol(), GameRules())
        if all(len(player.hand) == 0 for player in state.players) and state.phase == Phase.FINISHED:
            break
    return state


def test_same_seed_and_choices_give_identical_final_scores() -> None:
    first = _run_match(12345, ["a", "b"])
    second = _run_match(12345, ["a", "b"])

    assert first.model_dump_json() == second.model_dump_json()


def test_different_match_seed_gives_different_hands() -> None:
    first, _ = create_match("m_01", ["a", "b"], match_seed=12345)
    other, _ = create_match("m_01", ["a", "b"], match_seed=999)

    assert first.players[0].hand != other.players[0].hand


def test_hand_3_deal_is_determined_by_seed_not_prior_play() -> None:
    first, _ = create_match("m_01", ["a", "b"], match_seed=12345)
    first_hand3, _ = start_hand(first, hand_number=3)

    second, _ = create_match("m_02", ["a", "b"], match_seed=12345)
    second_hand3, _ = start_hand(second, hand_number=3)

    assert first_hand3.players[0].hand == second_hand3.players[0].hand
    assert first_hand3.players[0].hand != first.players[0].hand


def test_arena_games_are_identical_across_interpreter_hash_seeds(tmp_path: Path) -> None:
    """Reproducibility must never depend on dict or set iteration order."""
    config_file = tmp_path / "arena.json"
    config_file.write_text('{"catalogue": [{"bot": "random"}], "player_counts": [3]}')
    command = [
        sys.executable,
        "-c",
        "from sixnimmt.cli import app; app()",
        "play",
        "--config",
        str(config_file),
        "--games",
        "3",
        "--seed",
        "1234",
        "--json",
    ]
    outputs = set()

    for hash_seed in ("0", "1", "42", "12345"):
        # A subprocess is the point: PYTHONHASHSEED can only be varied at
        # interpreter start, so this cannot be exercised in-process.
        result = subprocess.run(  # noqa: S603
            [*command, "--output-dir", str(tmp_path / hash_seed)],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
        )
        assert result.returncode == 0
        run = load_run(tmp_path / hash_seed)
        outputs.add(
            tuple(
                (record.job_id, record.scores, record.winners, record.actions_accepted, record.actions_rejected)
                for record in run.results
            )
        )

    assert len(outputs) == 1
