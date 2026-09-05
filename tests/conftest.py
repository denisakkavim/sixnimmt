"""Shared fixtures for the HTTP server tests."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from sixnimmt_server.server.app import create_app

ADMIN_TOKEN = "admin-token-for-tests"  # noqa: S105
PLAYERS = ["alice", "bob", "cara"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def app() -> FastAPI:
    return create_app(admin_token=ADMIN_TOKEN)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as running:
        yield running


@dataclass
class Match:
    """A created match and the tokens minted for it."""

    client: TestClient
    match_id: str
    seed: int
    player_tokens: dict[str, str]
    public_spectator_token: str
    omniscient_token: str

    @property
    def players(self) -> list[str]:
        return list(self.player_tokens)

    def headers(self, who: str) -> dict[str, str]:
        """`who` is a player id, "spectator", "omniscient", or "admin"."""
        tokens = {
            **self.player_tokens,
            "spectator": self.public_spectator_token,
            "omniscient": self.omniscient_token,
            "admin": ADMIN_TOKEN,
        }
        return {"Authorization": f"Bearer {tokens[who]}"}

    def start(self) -> None:
        response = self.client.post(f"/matches/{self.match_id}/start", headers=self.headers("admin"))
        assert response.status_code == 200

    def state(self, who: str) -> dict[str, Any]:
        response = self.client.get(f"/matches/{self.match_id}/state", headers=self.headers(who))
        assert response.status_code == 200
        return response.json()

    def events(self, who: str, since: int = 0) -> list[dict[str, Any]]:
        response = self.client.get(f"/matches/{self.match_id}/events?since={since}", headers=self.headers(who))
        assert response.status_code == 200
        return response.json()["events"]

    def act(self, who: str, **payload: Any) -> Any:
        return self.client.post(f"/matches/{self.match_id}/actions", json=payload, headers=self.headers(who))

    def abandon(self) -> Any:
        return self.client.delete(f"/matches/{self.match_id}", headers=self.headers("admin"))


def open_match(
    client: TestClient,
    players: list[str] | list[dict[str, Any]] | None = None,
    seed: int = 12345,
    **body: Any,
) -> Match:
    """Create a match through the API and collect its tokens."""
    seats = players if players is not None else PLAYERS
    specs = [{"id": seat} if isinstance(seat, str) else seat for seat in seats]
    response = client.post(
        "/matches",
        json={"players": specs, "seed": seed, **body},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    created = response.json()
    return Match(
        client=client,
        match_id=created["match_id"],
        seed=created["seed"],
        player_tokens=created["player_tokens"],
        public_spectator_token=created["public_spectator_token"],
        omniscient_token=created["omniscient_token"],
    )


@pytest.fixture
def match(client: TestClient) -> Match:
    """A started three-player classic match."""
    started = open_match(client)
    started.start()
    return started


def advance_one_decision(match: Match) -> bool:
    """Take the next outstanding decision. False once the match is over."""
    table = match.state(match.players[0])
    if table["status"] != "in_progress":
        return False
    if table["awaiting"] is not None:
        response = match.act(table["awaiting"], type="choose_row", row_index=0)
        assert response.status_code == 200, response.text
        return True
    for player_id in match.players:
        view = match.state(player_id)
        # Classic mode keeps offering select_card to a committed player, so the
        # commitment flag rather than the advisory list decides who still owes a move.
        if "select_card" in view["legal_actions"] and not view["you"]["committed"]:
            response = match.act(player_id, type="select_card", card=view["you"]["hand"][0])
            assert response.status_code == 200, response.text
            return True
    return False


def play_to_completion(match: Match, limit: int = 5000) -> None:
    """Drive the match over HTTP until it finishes."""
    for _ in range(limit):
        if not advance_one_decision(match):
            return
    msg = "match did not finish within the decision limit"
    raise AssertionError(msg)
