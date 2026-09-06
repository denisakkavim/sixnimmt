"""Long polling and SSE: they may wake only on the subscriber's own stream."""

import json
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import anyio
import httpx2
import pytest
from conftest import ADMIN_TOKEN
from fastapi import FastAPI


@dataclass
class AsyncMatch:
    """A started classic match reachable over concurrent connections."""

    client: httpx2.AsyncClient
    match_id: str
    player_tokens: dict[str, str]

    def headers(self, who: str) -> dict[str, str]:
        tokens = {**self.player_tokens, "admin": ADMIN_TOKEN}
        return {"Authorization": f"Bearer {tokens[who]}"}

    async def state(self, who: str) -> dict[str, Any]:
        return (await self.client.get(f"/matches/{self.match_id}/state", headers=self.headers(who))).json()

    async def act(self, who: str, **payload: Any) -> httpx2.Response:
        return await self.client.post(f"/matches/{self.match_id}/actions", json=payload, headers=self.headers(who))

    async def wait(self, who: str, since: int, timeout: float) -> dict[str, Any]:
        response = await self.client.get(
            f"/matches/{self.match_id}/wait?since={since}&timeout={timeout}",
            headers=self.headers(who),
            timeout=timeout + 10,
        )
        return response.json()


@pytest.fixture
async def live(app: FastAPI) -> AsyncIterator[AsyncMatch]:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://server") as client:
        admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        created = await client.post(
            "/matches",
            json={"players": [{"id": "alice"}, {"id": "bob"}, {"id": "cara"}], "seed": 12345},
            headers=admin,
        )
        body = created.json()
        match = AsyncMatch(client=client, match_id=body["match_id"], player_tokens=body["player_tokens"])
        await client.post(f"/matches/{match.match_id}/start", headers=admin)
        yield match


async def _commit_alice(live: AsyncMatch) -> int:
    """Take Alice out of the decision she owes, so her long poll actually blocks."""
    view = await live.state("alice")
    await live.act("alice", type="select_card", card=view["you"]["hand"][0])
    return (await live.state("alice"))["view_version"]


async def _poll_into(live: AsyncMatch, version: int, timeout: float, response: dict[str, Any]) -> None:
    response.update(await live.wait("alice", since=version, timeout=timeout))


@pytest.mark.anyio
async def test_a_long_poll_returns_at_once_while_the_match_waits_on_the_caller(live: AsyncMatch) -> None:
    version = (await live.state("alice"))["view_version"]

    answered = await live.wait("alice", since=version, timeout=5)

    assert answered["timed_out"] is False
    assert "select_card" in answered["legal_actions"]


@pytest.mark.anyio
async def test_a_long_poll_returns_when_the_callers_own_stream_advances(live: AsyncMatch) -> None:
    version = await _commit_alice(live)
    bob_hand = (await live.state("bob"))["you"]["hand"]
    answered: dict[str, Any] = {}

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(_poll_into, live, version, 5, answered)
        await anyio.sleep(0.05)
        await live.act("bob", type="select_card", card=bob_hand[0])

    assert answered["timed_out"] is False
    assert [event["type"] for event in answered["events"]] == [
        "selection_registered",
        "player_committed",
    ]


@pytest.mark.anyio
async def test_another_players_rejected_action_never_wakes_a_long_poll(live: AsyncMatch) -> None:
    """The empty early return this prevents would itself announce hidden activity."""
    version = await _commit_alice(live)
    answered: dict[str, Any] = {}

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(_poll_into, live, version, 1, answered)
        await anyio.sleep(0.05)
        for _ in range(5):
            await live.act("bob", type="commit")

    assert answered["timed_out"] is True
    assert answered["events"] == []
    assert answered["view_version"] == version


@pytest.mark.anyio
async def test_a_long_poll_ends_when_the_match_is_abandoned(live: AsyncMatch) -> None:
    version = await _commit_alice(live)
    refusal: dict[str, Any] = {}

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(_poll_into, live, version, 30, refusal)
        await anyio.sleep(0.05)
        await live.client.delete(f"/matches/{live.match_id}", headers=live.headers("admin"))

    assert refusal["error"]["code"] == "MATCH_ABANDONED"


def read_sse(body: str) -> list[dict[str, Any]]:
    """The id and payload of each complete SSE message in a response body."""
    messages = []
    for block in body.split("\n\n"):
        lines = [line for line in block.splitlines() if not line.startswith(":")]
        fields = dict(line.split(": ", 1) for line in lines if ": " in line)
        if "data" in fields:
            messages.append({"id": fields.get("id"), "event": fields.get("event"), "data": json.loads(fields["data"])})
    return messages


@dataclass
class ServedMatch:
    """A started match behind a real HTTP server, so SSE behaves as it will in use."""

    base_url: str
    match_id: str
    player_tokens: dict[str, str]

    def headers(self, who: str) -> dict[str, str]:
        tokens = {**self.player_tokens, "admin": ADMIN_TOKEN}
        return {"Authorization": f"Bearer {tokens[who]}"}

    def events(self, who: str) -> list[dict[str, Any]]:
        with httpx2.Client() as client:
            response = client.get(f"{self.base_url}/matches/{self.match_id}/events", headers=self.headers(who))
        return response.json()["events"]

    def act(self, who: str, **payload: Any) -> Any:
        with httpx2.Client() as client:
            return client.post(
                f"{self.base_url}/matches/{self.match_id}/actions", json=payload, headers=self.headers(who)
            )

    def abandon(self) -> None:
        with httpx2.Client() as client:
            client.delete(f"{self.base_url}/matches/{self.match_id}", headers=self.headers("admin"))

    def stream(self, who: str, wanted: int, extra: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Read the caller's stream until it has produced `wanted` messages.

        A correct stream stays open and silent when there is nothing to send, so
        the read window is bounded here rather than waiting on the server.
        """
        body = ""
        headers = {**self.headers(who), **(extra or {})}
        with (
            httpx2.Client(timeout=3.0) as client,
            client.stream("GET", f"{self.base_url}/matches/{self.match_id}/stream", headers=headers) as response,
        ):
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            try:
                for chunk in response.iter_text():
                    body += chunk
                    if len(read_sse(body)) >= wanted:
                        break
            except httpx2.ReadTimeout:
                pass
        return read_sse(body)


@pytest.fixture
def streaming(served: str) -> ServedMatch:
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    with httpx2.Client() as client:
        created = client.post(
            f"{served}/matches",
            json={"players": [{"id": "alice"}, {"id": "bob"}, {"id": "cara"}], "seed": 12345},
            headers=admin,
        ).json()
        client.post(f"{served}/matches/{created['match_id']}/start", headers=admin)
    return ServedMatch(base_url=served, match_id=created["match_id"], player_tokens=created["player_tokens"])


def test_a_stream_replays_the_callers_own_events_numbered_by_their_cursor(streaming: ServedMatch) -> None:
    messages = streaming.stream("alice", wanted=6)

    assert [message["id"] for message in messages[:6]] == ["1", "2", "3", "4", "5", "6"]
    assert [message["event"] for message in messages[:6]] == [event["type"] for event in streaming.events("alice")[:6]]
    assert [message["data"]["view_version"] for message in messages[:6]] == [1, 2, 3, 4, 5, 6]
    assert all("seq" not in message["data"] for message in messages)


def test_a_stream_resumes_from_last_event_id_without_repeating(streaming: ServedMatch) -> None:
    messages = streaming.stream("alice", wanted=3, extra={"Last-Event-ID": "3"})

    assert [message["id"] for message in messages[:3]] == ["4", "5", "6"]


def test_a_stream_carries_no_event_the_subscriber_may_not_see(streaming: ServedMatch) -> None:
    for _ in range(5):
        streaming.act("bob", type="commit")
    visible = streaming.events("alice")

    messages = streaming.stream("alice", wanted=len(visible) + 1)

    delivered = [message["event"] for message in messages]
    assert "action_rejected" not in delivered
    assert delivered == [event["type"] for event in visible]


def test_a_stream_is_told_when_the_match_is_abandoned(streaming: ServedMatch) -> None:
    visible = streaming.events("alice")
    abandon = threading.Timer(0.5, streaming.abandon)
    abandon.start()

    messages = streaming.stream("alice", wanted=len(visible) + 2)

    abandon.cancel()
    assert [message["event"] for message in messages[-2:]] == ["match_abandoned", "match_closed"]
    assert messages[-1]["data"]["code"] == "MATCH_ABANDONED"
