"""Concurrent actions against one match: one serialized order, one consistent outcome."""

import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import anyio
import httpx2
import pytest
from conftest import ADMIN_TOKEN
from fastapi import FastAPI

from sixnimmt_server.server.sink import NullEventSink

pytestmark = pytest.mark.anyio


@dataclass
class AsyncMatch:
    """A negotiation-mode match driven over concurrent connections."""

    client: httpx2.AsyncClient
    match_id: str
    player_tokens: dict[str, str]

    def headers(self, who: str) -> dict[str, str]:
        tokens = {**self.player_tokens, "admin": ADMIN_TOKEN}
        return {"Authorization": f"Bearer {tokens[who]}"}

    async def state(self, who: str) -> dict[str, Any]:
        response = await self.client.get(f"/matches/{self.match_id}/state", headers=self.headers(who))
        return response.json()

    async def act(self, who: str, **payload: Any) -> httpx2.Response:
        return await self.client.post(f"/matches/{self.match_id}/actions", json=payload, headers=self.headers(who))


@pytest.fixture
async def negotiating(app: FastAPI) -> AsyncIterator[AsyncMatch]:
    """Three players, explicit commitment, so a final commit can be raced."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://server") as client:
        admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        created = await client.post(
            "/matches",
            json={
                "players": [{"id": "alice"}, {"id": "bob"}, {"id": "cara"}],
                "seed": 12345,
                "protocol": {"negotiation_enabled": True},
            },
            headers=admin,
        )
        body = created.json()
        match = AsyncMatch(client=client, match_id=body["match_id"], player_tokens=body["player_tokens"])
        await client.post(f"/matches/{match.match_id}/start", headers=admin)
        yield match


async def _select_all(match: AsyncMatch) -> None:
    for player_id in match.player_tokens:
        view = await match.state(player_id)
        await match.act(player_id, type="select_card", card=view["you"]["hand"][0])


async def _commit_all_but(match: AsyncMatch, held_back: list[str]) -> None:
    for player_id in match.player_tokens:
        if player_id not in held_back:
            await match.act(player_id, type="commit")


async def _run_together(*calls: Any) -> list[Any]:
    results: dict[int, Any] = {}

    async def run(index: int, call: Any) -> None:
        results[index] = await call()

    async with anyio.create_task_group() as tasks:
        for index, call in enumerate(calls):
            tasks.start_soon(run, index, call)
    return [results[index] for index in range(len(calls))]


async def test_two_final_commits_arriving_together_resolve_the_play_once(negotiating: AsyncMatch) -> None:
    await _select_all(negotiating)
    await _commit_all_but(negotiating, held_back=["bob", "cara"])

    responses = await _run_together(
        lambda: negotiating.act("bob", type="commit"),
        lambda: negotiating.act("cara", type="commit"),
    )

    assert [response.status_code for response in responses] == [200, 200]
    table = await negotiating.state("alice")
    # One reveal, not two: whichever commit linearized last resolved the play,
    # and the other could not resolve it again.
    assert len(table["revealed_this_hand"]) == 1


async def test_a_final_commit_racing_an_uncommit_yields_one_consistent_outcome(negotiating: AsyncMatch) -> None:
    await _select_all(negotiating)
    await _commit_all_but(negotiating, held_back=["cara"])

    commit, uncommit = await _run_together(
        lambda: negotiating.act("cara", type="commit"),
        lambda: negotiating.act("alice", type="uncommit"),
    )

    table = await negotiating.state("bob")
    if commit.status_code == 200 and uncommit.status_code != 200:
        # The commit linearized first: the play is irrevocably committed, and no
        # later action can withdraw a selection from it.
        assert len(table["revealed_this_hand"]) == 1
    else:
        # The uncommit linearized first, so Cara's commit was no longer the last.
        assert uncommit.status_code == 200
        assert table["revealed_this_hand"] == []


async def test_a_final_commit_racing_a_selection_yields_one_consistent_outcome(negotiating: AsyncMatch) -> None:
    await _select_all(negotiating)
    await _commit_all_but(negotiating, held_back=["cara"])
    alice_hand = (await negotiating.state("alice"))["you"]["hand"]

    commit, select = await _run_together(
        lambda: negotiating.act("cara", type="commit"),
        lambda: negotiating.act("alice", type="select_card", card=alice_hand[1]),
    )

    table = await negotiating.state("bob")
    if commit.status_code == 200 and select.status_code != 200:
        assert len(table["revealed_this_hand"]) == 1
    else:
        # Alice's re-selection cleared her commitment before Cara's commit landed.
        assert select.status_code == 200
        assert table["revealed_this_hand"] == []


async def test_a_final_commit_racing_a_message_leaves_the_match_consistent(negotiating: AsyncMatch) -> None:
    """A message belongs to the selecting play at its serialization point."""
    await _select_all(negotiating)
    await _commit_all_but(negotiating, held_back=["cara"])

    commit, message = await _run_together(
        lambda: negotiating.act("cara", type="commit"),
        lambda: negotiating.act("alice", type="send_message", visibility="table", body="please take row 2"),
    )

    assert commit.status_code == 200
    if message.status_code != 200:
        assert message.json()["error"]["code"] in {"NOT_YOUR_TURN", "MATCH_FINISHED"}
    else:
        assert message.json()["messages"][-1]["body"] == "please take row 2"
    table = await negotiating.state("bob")
    assert len(table["revealed_this_hand"]) == 1


async def test_a_stale_expected_view_version_is_refused(negotiating: AsyncMatch) -> None:
    view = await negotiating.state("alice")

    response = await negotiating.act(
        "alice", type="select_card", card=view["you"]["hand"][0], expected_view_version=view["view_version"] - 1
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VERSION_CONFLICT"
    assert (await negotiating.state("alice"))["you"]["selection"] is None


async def test_a_current_expected_view_version_is_accepted(negotiating: AsyncMatch) -> None:
    view = await negotiating.state("alice")

    response = await negotiating.act(
        "alice", type="select_card", card=view["you"]["hand"][0], expected_view_version=view["view_version"]
    )

    assert response.status_code == 200


async def test_a_visible_action_landing_first_turns_the_guard_into_a_conflict(negotiating: AsyncMatch) -> None:
    """The check runs at the linearization point, so nothing can slip in behind it."""
    alice = await negotiating.state("alice")
    bob_hand = (await negotiating.state("bob"))["you"]["hand"]

    bob, guarded = await _run_together(
        lambda: negotiating.act("bob", type="select_card", card=bob_hand[0]),
        lambda: negotiating.act(
            "alice",
            type="select_card",
            card=alice["you"]["hand"][0],
            expected_view_version=alice["view_version"],
        ),
    )

    assert bob.status_code == 200
    if guarded.status_code == 200:
        # Alice linearized first, so nothing she could see had changed.
        assert (await negotiating.state("alice"))["you"]["selection"] == alice["you"]["hand"][0]
    else:
        # Bob's selection_registered is public, so Alice's cursor had moved.
        assert guarded.json()["error"]["code"] == "VERSION_CONFLICT"
        assert (await negotiating.state("alice"))["you"]["selection"] is None


class SlowSink:
    """A sink whose durable write takes real time, as a loaded disk would."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def append(self, events: Sequence[Any]) -> None:
        time.sleep(self.seconds)

    def record_action(self, record: Any) -> None:
        return

    def close(self) -> None:
        return


async def test_one_matchs_slow_disk_does_not_hold_up_another_match(app: FastAPI) -> None:
    """Waiting for a write must not stop the server serving everyone else."""
    write_seconds = 0.3
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://server") as client:
        admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        matches = []
        for _ in range(2):
            created = await client.post(
                "/matches",
                json={"players": [{"id": "alice"}, {"id": "bob"}], "seed": 12345},
                headers=admin,
            )
            body = created.json()
            match = AsyncMatch(client=client, match_id=body["match_id"], player_tokens=body["player_tokens"])
            await client.post(f"/matches/{match.match_id}/start", headers=admin)
            app.state.store.record_for(match.match_id).sink = SlowSink(write_seconds)
            matches.append(match)

        cards = [(await match.state("alice"))["you"]["hand"][0] for match in matches]
        started = time.perf_counter()
        responses = await _run_together(
            lambda: matches[0].act("alice", type="select_card", card=cards[0]),
            lambda: matches[1].act("alice", type="select_card", card=cards[1]),
        )
        elapsed = time.perf_counter() - started

    assert [response.status_code for response in responses] == [200, 200]
    # Overlapped they cost about one write; serialised on the event loop they
    # would cost both, so the margin has to sit clearly between the two.
    assert elapsed < write_seconds * 1.8


async def test_a_caller_that_gives_up_mid_write_still_completes_its_transition(app: FastAPI) -> None:
    """A dispatched durable write cannot be recalled, so the match must not be left behind it.

    Abandoning the transition here would leave the log holding a batch the
    match never committed, and the next action reusing its sequence numbers.
    """
    write_seconds = 0.3
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://server") as client:
        admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        created = await client.post(
            "/matches",
            json={"players": [{"id": "alice"}, {"id": "bob"}], "seed": 12345},
            headers=admin,
        )
        body = created.json()
        match = AsyncMatch(client=client, match_id=body["match_id"], player_tokens=body["player_tokens"])
        await client.post(f"/matches/{match.match_id}/start", headers=admin)
        record = app.state.store.record_for(match.match_id)
        card = (await match.state("alice"))["you"]["hand"][0]
        record.sink = SlowSink(write_seconds)

        with anyio.move_on_after(write_seconds / 4):
            await match.act("alice", type="select_card", card=card)

        record.sink = NullEventSink()
        await anyio.sleep(write_seconds)
        assert (await match.state("alice"))["you"]["selection"] == card


@pytest.mark.parametrize("message_first", [True, False])
async def test_guarded_message_and_final_commit_have_one_serialized_winner(
    negotiating: AsyncMatch, message_first: bool
) -> None:
    await _select_all(negotiating)
    await _commit_all_but(negotiating, held_back=["cara"])
    version = (await negotiating.state("cara"))["view_version"]
    message = lambda: negotiating.act(
        "cara", type="send_message", visibility="table", body="wait", expected_view_version=version
    )
    commit = lambda: negotiating.act("cara", type="commit", expected_view_version=version)
    calls = (message, commit) if message_first else (commit, message)
    results = await _run_together(*calls)
    assert sorted(response.status_code for response in results) == [200, 409]
    rejected = next(response for response in results if response.status_code == 409)
    assert rejected.json()["error"]["code"] == "VERSION_CONFLICT"
    message_result = results[0 if message_first else 1]
    table = await negotiating.state("alice")
    assert len(table["revealed_this_hand"]) == (0 if message_result.status_code == 200 else 1)
