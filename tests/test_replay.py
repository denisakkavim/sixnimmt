"""Replaying a log rebuilds the state that produced it, field for field."""

import pytest

from sixnimmt_server.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt_server.engine.events import ActionRejectedEvent, Event, MatchAbandonedEvent
from sixnimmt_server.engine.replay import replay_events
from sixnimmt_server.engine.rules import GameRules, MatchProtocol
from sixnimmt_server.engine.setup import create_match
from sixnimmt_server.engine.state import MatchState, Phase, PlayerSeat
from sixnimmt_server.engine.transition import transition

CLASSIC = MatchProtocol()
COMMUNICATION = MatchProtocol(communication_enabled=True)


def _next_decision(state: MatchState, row_choice: int) -> tuple[str, Action]:
    """Whoever the match is waiting on, and the simplest move they can make."""
    if state.phase == Phase.AWAITING_ROW_CHOICE:
        assert state.resolution is not None and state.resolution.awaiting_player is not None
        return state.resolution.awaiting_player, ChooseRowAction(row_index=row_choice)
    unselected = next((player for player in state.players if player.selection is None), None)
    if unselected is not None:
        return unselected.player_id, SelectCardAction(card=unselected.hand[0])
    # Only reachable with communication on, where selecting does not commit.
    uncommitted = next(player for player in state.players if not player.committed)
    return uncommitted.player_id, CommitAction()


def _checkpoints(
    player_ids: list[str],
    seed: int = 12345,
    protocol: MatchProtocol = CLASSIC,
    row_choice: int = 0,
) -> list[tuple[MatchState, list[Event]]]:
    """Every (live state, log so far) pair of a complete match."""
    state, events = create_match("m_01", player_ids, match_seed=seed, protocol=protocol)
    log = list(events)
    history = [(state, list(log))]
    while state.phase != Phase.FINISHED:
        actor, action = _next_decision(state, row_choice)
        state, produced = transition(state, actor, action, protocol, GameRules())
        log.extend(produced)
        history.append((state, list(log)))
    return history


def _comparable(state: MatchState) -> dict:
    """The live state in the form a log can reconstruct it.

    Only the undealt remainder is normalised: the deal records which cards went
    to hands and rows, but nothing records the order of the cards it never used.
    """
    dumped = state.model_dump()
    dumped["undealt_remainder"] = sorted(dumped["undealt_remainder"])
    return dumped


@pytest.mark.parametrize("player_count", [2, 3, 10])
def test_replaying_a_full_match_reproduces_the_final_state_field_for_field(player_count: int) -> None:
    player_ids = [f"player_{index}" for index in range(player_count)]
    live, log = _checkpoints(player_ids)[-1]

    replayed = replay_events(log)

    assert _comparable(replayed.state) == _comparable(live)


def test_replay_matches_live_state_after_every_transition() -> None:
    for live, log in _checkpoints(["alice", "bob", "cara"]):
        replayed = replay_events(log)

        assert _comparable(replayed.state) == _comparable(live)


@pytest.mark.parametrize("row_choice", [0, 1, 2, 3])
def test_replay_follows_a_row_choice_onto_whichever_row_was_chosen(row_choice: int) -> None:
    live, log = _checkpoints(["alice", "bob", "cara"], seed=4242, row_choice=row_choice)[-1]

    replayed = replay_events(log)

    assert _comparable(replayed.state) == _comparable(live)


def test_replay_reports_the_winners_the_log_recorded() -> None:
    live, log = _checkpoints(["alice", "bob"])[-1]
    lowest = min(player.total_score for player in live.players)

    replayed = replay_events(log)

    assert replayed.status == "finished"
    assert replayed.winners == tuple(
        sorted(player.player_id for player in live.players if player.total_score == lowest)
    )


def test_replay_recovers_the_match_seed_from_the_admin_log() -> None:
    _, log = _checkpoints(["alice", "bob"], seed=777)[0]

    assert replay_events(log).state.match_seed == 777


def test_a_log_without_admin_events_replays_without_the_seed() -> None:
    """A player never receives the seed, so a stream filtered for one cannot leak it."""
    _, log = _checkpoints(["alice", "bob"], seed=777)[-1]
    public_only = [event for event in log if event.audience != "admin"]

    assert replay_events(public_only).state.match_seed is None


def test_a_rejected_action_folds_to_no_state_change() -> None:
    live, log = _checkpoints(["alice", "bob"])[4]
    before = replay_events(log).state
    rejected: Event = ActionRejectedEvent(
        match_id=live.match_id,
        hand=live.hand_number,
        play=live.play_number,
        audience="player:alice",
        data={"code": "CARD_NOT_IN_HAND", "message": "not in hand", "action_type": "select_card"},
    )

    after = replay_events([*log, rejected]).state

    assert after == before


def test_an_abandoned_match_replays_as_abandoned_without_a_winner() -> None:
    live, log = _checkpoints(["alice", "bob"])[4]
    abandoned: Event = MatchAbandonedEvent(
        match_id=live.match_id,
        hand=live.hand_number,
        play=live.play_number,
        audience="public",
        data={},
    )

    replayed = replay_events([*log, abandoned])

    assert replayed.status == "abandoned"
    assert replayed.winners == ()
    # Abandonment is a server lifecycle event, so it leaves the game itself
    # exactly where it stood rather than moving it through a phase.
    assert _comparable(replayed.state) == _comparable(live)


def test_replay_carries_the_agent_metadata_the_admin_log_recorded() -> None:
    seats = [
        PlayerSeat(player_id="alice", display_name="Player 1", agent_metadata={"model": "claude-opus-5"}),
        PlayerSeat(player_id="bob", display_name="Player 2", agent_metadata={"model": "baseline"}),
    ]
    state, log = create_match("m_01", seats, match_seed=12345)

    replayed = replay_events(log)

    assert replayed.state.players == state.players


def test_every_field_replays_after_each_communication_transition() -> None:
    for live, log in _checkpoints(["alice", "bob"], seed=77, protocol=COMMUNICATION):
        assert _comparable(replay_events(log).state) == _comparable(live)
