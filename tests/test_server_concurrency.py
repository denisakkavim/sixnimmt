"""Concurrent actions against one match: one serialized order, one consistent outcome."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import anyio
import httpx2
import pytest
from conftest import ADMIN_TOKEN
from fastapi import FastAPI

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
    """Messaging is refused until phase 6; the race must still resolve cleanly."""
    await _select_all(negotiating)
    await _commit_all_but(negotiating, held_back=["cara"])

    commit, message = await _run_together(
        lambda: negotiating.act("cara", type="commit"),
        lambda: negotiating.act("alice", type="send_message", visibility="table", body="please take row 2"),
    )

    assert commit.status_code == 200
    assert message.status_code == 409
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
