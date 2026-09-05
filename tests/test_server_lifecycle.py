"""Abandonment, refusals, and staying healthy against agents that misbehave."""

import pytest
from conftest import Match, advance_one_decision, open_match, play_to_completion
from starlette.testclient import TestClient

from sixnimmt_server.server.errors import ApiErrorCode

# The codes §9.4 requires the server to be able to produce.
DOCUMENTED_CODES = {
    ApiErrorCode.MATCH_NOT_FOUND,
    ApiErrorCode.NOT_AUTHORIZED,
    ApiErrorCode.MATCH_NOT_STARTED,
    ApiErrorCode.MATCH_ALREADY_STARTED,
    ApiErrorCode.MATCH_FINISHED,
    ApiErrorCode.MATCH_ABANDONED,
    ApiErrorCode.WRONG_PHASE,
    ApiErrorCode.NOT_YOUR_TURN,
    ApiErrorCode.CARD_NOT_IN_HAND,
    ApiErrorCode.NO_SELECTION_TO_COMMIT,
    ApiErrorCode.CANNOT_UNCOMMIT_WHEN_ALL_COMMITTED,
    ApiErrorCode.NEGOTIATION_DISABLED,
    ApiErrorCode.INVALID_ROW_INDEX,
    ApiErrorCode.ROW_ALREADY_CHOSEN,
    ApiErrorCode.ACTION_BUDGET_EXHAUSTED,
    ApiErrorCode.MESSAGE_TOO_LONG,
    ApiErrorCode.DIRECT_MESSAGES_DISABLED,
    ApiErrorCode.RECIPIENT_NOT_FOUND,
    ApiErrorCode.VERSION_CONFLICT,
    ApiErrorCode.IDEMPOTENCY_KEY_REUSED,
    ApiErrorCode.MALFORMED_REQUEST,
    ApiErrorCode.UNKNOWN_ACTION_TYPE,
}


def play_until_row_choice(match: Match) -> str:
    """Advance until someone must choose a row, and name them."""
    for _ in range(500):
        table = match.state(match.players[0])
        if table["awaiting"] is not None:
            return str(table["awaiting"])
        assert advance_one_decision(match)
    msg = "no row choice arose"
    raise AssertionError(msg)


def negotiate_until_row_choice(match: Match) -> None:
    """Play a negotiation match until a row choice pauses a committed play."""
    for _ in range(500):
        if match.state(match.players[0])["awaiting"] is not None:
            return
        for player_id in match.players:
            view = match.state(player_id)
            if not view["you"]["committed"] and view["phase"] == "selecting":
                match.act(player_id, type="select_card", card=view["you"]["hand"][0])
                match.act(player_id, type="commit")
    msg = "no row choice arose"
    raise AssertionError(msg)


def test_abandoning_a_match_appends_a_public_event(match: Match) -> None:
    response = match.abandon()

    assert response.status_code == 204
    for who in [*match.players, "spectator"]:
        assert match.events(who)[-1]["type"] == "match_abandoned"
        assert match.state(who)["status"] == "abandoned"


def test_actions_after_abandonment_are_refused(match: Match) -> None:
    card = match.state("alice")["you"]["hand"][0]
    match.abandon()

    response = match.act("alice", type="select_card", card=card)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MATCH_ABANDONED"


def test_the_event_log_survives_abandonment(match: Match) -> None:
    before = len(match.events("alice"))

    match.abandon()

    assert len(match.events("alice")) == before + 1
    assert match.client.get("/matches", headers=match.headers("admin")).json()["matches"]


def test_abandoning_twice_is_harmless(match: Match) -> None:
    assert match.abandon().status_code == 204
    assert match.abandon().status_code == 204

    abandonments = [event for event in match.events("alice") if event["type"] == "match_abandoned"]
    assert len(abandonments) == 1


def test_a_match_that_nobody_commits_to_stays_valid_indefinitely(match: Match) -> None:
    """Non-progress is the harness's problem; the server's job is to stay correct."""
    before = match.state("alice")

    for _ in range(200):
        match.act("bob", type="commit")

    after = match.state("alice")
    assert after["phase"] == "selecting"
    assert after["you"] == before["you"]
    assert after["view_version"] == before["view_version"]
    assert all(seat["cards_in_hand"] == 10 for seat in after["players"])


def test_a_match_completes_against_an_agent_that_misbehaves_before_every_move(match: Match) -> None:
    while True:
        match.act("alice", type="select_card", card=999)
        match.act("alice", type="choose_row", row_index=99)
        match.act("alice", type="nonsense")
        if not advance_one_decision(match):
            break

    final = match.state("alice")
    assert final["status"] == "finished"
    assert max(seat["total_score"] for seat in [*final["players"], final["you"]]) >= 66


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"type": "select_card"}, id="missing_card"),
        pytest.param({"type": "choose_row", "row_index": "left"}, id="wrong_type"),
        pytest.param({}, id="empty_body"),
        pytest.param({"type": 17}, id="numeric_type"),
    ],
)
def test_a_malformed_action_is_refused_without_touching_the_match(match: Match, payload: dict) -> None:
    before = match.state("alice")

    response = match.act("alice", **payload)

    assert response.status_code == 400
    assert match.state("alice") == before


def test_every_documented_error_code_is_produced_by_a_real_scenario(client: TestClient) -> None:
    produced: set[str] = set()

    def note(response) -> None:
        if response.status_code >= 400:
            produced.add(response.json()["error"]["code"])

    unstarted = open_match(client, ["alice", "bob"])
    note(unstarted.act("alice", type="select_card", card=1))  # MATCH_NOT_STARTED
    unstarted.start()
    note(unstarted.client.post(f"/matches/{unstarted.match_id}/start", headers=unstarted.headers("admin")))
    note(client.get("/matches/m_absent/state", headers=unstarted.headers("alice")))  # MATCH_NOT_FOUND
    note(unstarted.act("spectator", type="commit"))  # NOT_AUTHORIZED

    hand = unstarted.state("alice")["you"]["hand"]
    unheld = next(card for card in range(1, 105) if card not in hand)
    note(unstarted.act("alice", type="select_card", card=unheld))  # CARD_NOT_IN_HAND
    note(unstarted.act("alice", type="commit"))  # NO_SELECTION_TO_COMMIT
    note(unstarted.act("alice", type="uncommit"))  # NEGOTIATION_DISABLED
    note(unstarted.act("alice", type="choose_row", row_index=0))  # WRONG_PHASE
    note(unstarted.act("alice", type="wave"))  # UNKNOWN_ACTION_TYPE
    note(unstarted.act("alice", type="select_card"))  # MALFORMED_REQUEST
    note(unstarted.act("alice", type="select_card", card=hand[0], expected_view_version=1))  # VERSION_CONFLICT
    unstarted.act("alice", type="select_card", card=hand[0], action_id="dup")
    note(unstarted.act("alice", type="select_card", card=hand[1], action_id="dup"))  # IDEMPOTENCY_KEY_REUSED

    negotiating = open_match(client, ["alice", "bob"], protocol={"negotiation_enabled": True})
    negotiating.start()
    note(negotiating.act("alice", type="send_message", visibility="table", body="x" * 3000))  # MESSAGE_TOO_LONG
    note(negotiating.act("alice", type="send_message", visibility="direct", to_player="ghost", body="hi"))
    negotiate_until_row_choice(negotiating)
    # Everyone has committed and the play is mid-resolution, so there is nothing
    # left for anyone to withdraw.
    note(negotiating.act("alice", type="uncommit"))  # CANNOT_UNCOMMIT_WHEN_ALL_COMMITTED

    quiet = open_match(client, ["alice", "bob"], protocol={"allow_direct_messages": False, "negotiation_enabled": True})
    quiet.start()
    note(quiet.act("alice", type="send_message", visibility="direct", to_player="bob", body="hi"))

    budgeted = open_match(client, ["alice", "bob"], protocol={"max_actions_per_play": 1})
    budgeted.start()
    budget_hand = budgeted.state("alice")["you"]["hand"]
    budgeted.act("alice", type="select_card", card=budget_hand[0])
    note(budgeted.act("alice", type="select_card", card=budget_hand[1]))  # ACTION_BUDGET_EXHAUSTED

    choosing = open_match(client, ["alice", "bob", "cara"])
    choosing.start()
    chooser = play_until_row_choice(choosing)
    other = next(player for player in choosing.players if player != chooser)
    note(choosing.act(other, type="choose_row", row_index=0))  # NOT_YOUR_TURN
    note(choosing.act(chooser, type="choose_row", row_index=9))  # INVALID_ROW_INDEX
    choosing.act(chooser, type="choose_row", row_index=0)
    note(choosing.act(chooser, type="choose_row", row_index=1))  # ROW_ALREADY_CHOSEN
    play_to_completion(choosing)
    note(choosing.act("alice", type="select_card", card=1))  # MATCH_FINISHED

    doomed = open_match(client, ["alice", "bob"])
    doomed.start()
    doomed.abandon()
    note(doomed.act("alice", type="commit"))  # MATCH_ABANDONED

    assert {code.value for code in DOCUMENTED_CODES} - produced == set()


@pytest.mark.parametrize(
    ("players", "expected"),
    [
        pytest.param([{"id": "solo"}], "INVALID_PLAYER_COUNT", id="too_few"),
        pytest.param([{"id": "a"}, {"id": "a"}], "DUPLICATE_PLAYER_ID", id="duplicates"),
        pytest.param([{"id": ""}, {"id": "b"}], "INVALID_PLAYER_ID", id="empty_id"),
    ],
)
def test_a_badly_specified_match_is_refused_at_creation(client: TestClient, players: list[dict], expected: str) -> None:
    response = client.post(
        "/matches",
        json={"players": players, "seed": 1},
        headers={"Authorization": "Bearer admin-token-for-tests"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == expected


def test_a_pathological_agent_leaves_the_match_playable(match: Match) -> None:
    """Nothing a hostile client sends may corrupt state or stop the game."""
    for round_number in range(50):
        match.act("bob", type="select_card", card=105)
        match.act("bob", type="choose_row", row_index=-1)
        match.act("bob", type="uncommit")
        match.act("bob", type="send_message", visibility="table", body="x" * 5000)
        match.act("bob", type="commit", action_id="always-the-same")
        match.act("bob", type="select_card", card=1, expected_view_version=0)
        match.act("bob", type="???")
        assert advance_one_decision(match) or round_number > 0

    play_to_completion(match)
    assert match.state("alice")["status"] == "finished"


def test_a_bot_that_flips_between_commit_and_uncommit_cannot_stall_the_server(client: TestClient) -> None:
    flipping = open_match(client, ["alice", "bob"], protocol={"negotiation_enabled": True})
    flipping.start()
    card = flipping.state("bob")["you"]["hand"][0]

    # Uncommitting clears the selection too, so a flip-flop is really a cycle of
    # three actions rather than two.
    for _ in range(100):
        assert flipping.act("bob", type="select_card", card=card).status_code == 200
        assert flipping.act("bob", type="commit").status_code == 200
        assert flipping.act("bob", type="uncommit").status_code == 200

    table = flipping.state("alice")
    assert table["phase"] == "selecting"
    assert table["revealed_this_hand"] == []
    assert len(flipping.state("bob")["you"]["hand"]) == 10


def test_a_stale_from_view_never_costs_the_caller_anything(match: Match) -> None:
    """It is an audit reference, not a concurrency token (§9.3)."""
    card = match.state("alice")["you"]["hand"][0]

    response = match.act("alice", type="select_card", card=card, from_view="v_from_a_previous_match")

    assert response.status_code == 200
    assert response.json()["you"]["selection"] == card
