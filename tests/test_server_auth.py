"""Tokens, roles, and the refusal that must not tell a caller which matches exist."""

import pytest
from conftest import ADMIN_TOKEN, Match, open_match
from starlette.testclient import TestClient


def test_creation_returns_a_token_for_every_seat_and_role(client: TestClient) -> None:
    created = open_match(client, ["alice", "bob"])

    minted = {*created.player_tokens.values(), created.public_spectator_token, created.omniscient_token}

    assert set(created.player_tokens) == {"alice", "bob"}
    assert len(minted) == 4


def test_match_creation_requires_an_admin_token(client: TestClient) -> None:
    response = client.post(
        "/matches",
        json={"players": [{"id": "alice"}, {"id": "bob"}], "seed": 1},
        headers={"Authorization": "Bearer not-the-admin-token"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHORIZED"


def test_seed_is_returned_at_creation_and_nowhere_else(client: TestClient) -> None:
    created = open_match(client, seed=987654321)
    created.start()

    assert created.seed == 987654321
    for who in [*created.players, "spectator", "omniscient"]:
        assert (
            "987654321"
            not in created.client.get(f"/matches/{created.match_id}/events", headers=created.headers(who)).text
        )


@pytest.mark.parametrize(
    "role",
    ["alice", "spectator", "omniscient"],
    ids=["player", "public_spectator", "omniscient_observer"],
)
def test_only_admin_may_list_matches(match: Match, role: str) -> None:
    response = match.client.get("/matches", headers=match.headers(role))

    assert response.status_code == 401


def test_admin_listing_reports_status_and_scores(match: Match) -> None:
    listed = match.client.get("/matches", headers=match.headers("admin")).json()["matches"]

    entry = next(summary for summary in listed if summary["match_id"] == match.match_id)
    assert entry["status"] == "in_progress"
    assert entry["scores"] == dict.fromkeys(match.players, 0)


def test_a_players_token_cannot_read_another_match(client: TestClient) -> None:
    mine = open_match(client, ["alice", "bob"])
    theirs = open_match(client, ["cara", "dan"])

    response = client.get(f"/matches/{theirs.match_id}/state", headers=mine.headers("alice"))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MATCH_NOT_FOUND"


def test_a_foreign_match_is_indistinguishable_from_one_that_never_existed(client: TestClient) -> None:
    mine = open_match(client, ["alice", "bob"])
    theirs = open_match(client, ["cara", "dan"])

    foreign = client.get(f"/matches/{theirs.match_id}/state", headers=mine.headers("alice"))
    absent = client.get("/matches/m_0000000000000000/state", headers=mine.headers("alice"))

    assert foreign.status_code == absent.status_code
    assert foreign.json()["error"]["code"] == absent.json()["error"]["code"]
    assert foreign.json()["error"].keys() == absent.json()["error"].keys()


def test_a_player_cannot_obtain_the_omniscient_view_of_their_own_match(match: Match) -> None:
    """Role travels with the token, so there is nothing to ask for."""
    as_player = match.state("alice")
    as_omniscient = match.state("omniscient")

    assert as_player["you"]["player_id"] == "alice"
    assert as_omniscient["you"]["player_id"] == ""
    assert len(as_omniscient["players"]) == len(match.players)


def test_a_missing_token_is_refused(match: Match) -> None:
    response = match.client.get(f"/matches/{match.match_id}/state")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MATCH_NOT_FOUND"


def test_only_a_player_token_may_submit_actions(match: Match) -> None:
    for who in ("spectator", "omniscient", "admin"):
        response = match.act(who, type="commit")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "NOT_AUTHORIZED"


def test_admin_token_reads_any_match(client: TestClient) -> None:
    created = open_match(client, ["alice", "bob"])
    created.start()

    response = client.get(f"/matches/{created.match_id}/state", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})

    assert response.status_code == 200
