"""A complete arena game through the real local MCP subprocess, without a model."""

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import anyio
import pytest
from mcp import Client, StdioServerParameters

from sixnimmt.arena.bots.external import HarnessBot
from sixnimmt.arena.bots.external_harnesses.broker import SeatSession
from sixnimmt.arena.bots.external_harnesses.mcp import MCP_PROTOCOL_VERSION
from sixnimmt.arena.bots.external_harnesses.transport import ControllerServer
from sixnimmt.arena.bots.heuristics import LowestFittingCardBot
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.match import run_match
from sixnimmt.arena.results import MatchOutcome
from sixnimmt.engine.replay import replay_events
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.state import PlayerSeat
from sixnimmt.engine.views import MatchView


async def _play_through_mcp(endpoint: str, session: SeatSession, directory: Path) -> dict[str, Any]:
    process = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sixnimmt.arena.bots.external_harnesses.mcp"],
        cwd=directory,
        env={"SIXNIMMT_ENDPOINT": endpoint, "SIXNIMMT_CREDENTIAL": session.credential},
    )
    with anyio.fail_after(15):
        async with Client(process, mode=MCP_PROTOCOL_VERSION, read_timeout_seconds=3) as client:
            assert client.protocol_version == MCP_PROTOCOL_VERSION
            info = await client.call_tool("get_game_info", {"session_id": session.session_id})
            assert not info.is_error
            assert info.structured_content["player_id"] == "external"
            strategy = LowestFittingCardBot()
            proposal: dict[str, Any] | None = None
            for index in range(100):
                result = await client.call_tool("play", {"session_id": session.session_id, "proposal": proposal})
                assert not result.is_error, result.content
                response = result.structured_content
                assert isinstance(response, dict)
                if proposal is not None:
                    assert response["receipt"]["submission_id"] == proposal["submission_id"]
                    assert response["receipt"]["status"] == "accepted"
                if response["status"] == "terminal":
                    return response
                assert response["status"] == "decision"
                offer = response["offer"]
                view = MatchView.model_validate(offer["view"])
                action = strategy.act(view)
                proposal = {
                    "protocol_version": offer["protocol_version"],
                    "session_id": offer["session_id"],
                    "decision_id": offer["decision_id"],
                    "submission_id": f"submission-{index}",
                    "view_id": offer["view_id"],
                    "actions": [
                        action.model_dump(mode="json", exclude={"action_id", "from_view", "expected_view_version"})
                    ],
                    "memory": None,
                }
    pytest.fail("The MCP player did not finish within its decision bound")


@pytest.mark.parametrize("communication", [False, True], ids=["classic", "communication"])
def test_mcp_player_finishes_with_accepted_receipt_and_replay_equivalent_state(
    tmp_path: Path, communication: bool
) -> None:
    rules = GameRules()
    protocol = MatchProtocol(end_condition="fixed_hands", hands=1, communication_enabled=communication)
    seats = [PlayerSeat(player_id="external"), PlayerSeat(player_id="baseline")]
    session = SeatSession("external", "External player", rules, protocol, wait_timeout_seconds=5)
    stopped = Event()

    with ControllerServer([session]) as controller, ThreadPoolExecutor(max_workers=1) as worker:
        match = worker.submit(
            run_match,
            [HarnessBot(session), LowestFittingCardBot()],
            seed=123,
            match_id="mcp-game",
            seats=seats,
            rules=rules,
            protocol=protocol,
            config=RunConfig(decision_timeout_seconds=10),
            stop_event=stopped,
        )
        try:
            terminal = anyio.run(_play_through_mcp, controller.endpoint, session, tmp_path)
            played = match.result(timeout=3)
        finally:
            stopped.set()
            session.stop()

    assert terminal["result"]["outcome"] == "finished"
    assert terminal["receipt"]["status"] == "accepted"
    assert terminal["offer"] is None
    assert played.outcome == MatchOutcome.FINISHED
    assert played.actions_rejected == 0

    baseline = run_match(
        [LowestFittingCardBot(), LowestFittingCardBot()],
        seed=123,
        match_id="mcp-game",
        seats=seats,
        rules=rules,
        protocol=protocol,
    )
    assert played.final_state == baseline.final_state
    replayed = replay_events(played.events)
    # The event log records the undealt cards but not their unused deck order.
    replayable = played.final_state.model_copy(
        update={"undealt_remainder": tuple(sorted(played.final_state.undealt_remainder))}
    )
    assert replayed.state == replayable
    assert replayed.winners == played.winners
