"""Experiment outcomes, privacy, trace provenance, and bounded provider failures."""

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from sixnimmt.analytics.summary import summarise
from sixnimmt.arena.bots import REGISTRY, BotSpec, LowestFittingCardBot, RandomBot, Rejection
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.arena.runner import ArenaError, MatchOutcome, RunConfig, resolve, run_arena, run_match
from sixnimmt.arena.scheduling import SequentialScheduler
from sixnimmt.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction, SendMessageAction
from sixnimmt.engine.audience import Viewer, visible_events
from sixnimmt.engine.events import Event
from sixnimmt.engine.fold import ViewFolder, build_view
from sixnimmt.engine.replay import replay_events
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.engine.state import MatchState, Phase
from sixnimmt.engine.views import MatchView, ViewRole
from sixnimmt.persistence.manifest import ManifestMatch
from sixnimmt.persistence.sink import read_action_log, read_event_log


class BadRows(RandomBot):
    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if view.phase == Phase.AWAITING_ROW_CHOICE:
            return ChooseRowAction(row_index=99)
        return super().act(view, rejection)


class Malformed:
    def act(self, view: MatchView, rejection: Rejection | None = None) -> Any:
        return {"card": 10}


class HiddenSender(RandomBot):
    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if view.you.actions_taken_this_play == 0:
            return SendMessageAction(visibility="direct", to_player="player_2", body="secret")
        return super().act(view, rejection)


class Chatty(RandomBot):
    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        if view.you.actions_taken_this_play == 0:
            return SendMessageAction(visibility="direct", to_player="player_2", body="hello")
        return super().act(view, rejection)


def _fail_bot_build(seed: int) -> RandomBot:
    msg = "invalid provider configuration"
    raise ValueError(msg)


def _fail_scheduler(self: SequentialScheduler, state: MatchState) -> int | None:
    msg = "scheduler defect"
    raise RuntimeError(msg)


@pytest.fixture
def short_protocol() -> MatchProtocol:
    return MatchProtocol(end_condition="fixed_hands", hands=1)


class InvalidThenLegal(RandomBot):
    def __init__(self, seed: int) -> None:
        super().__init__(seed)
        self.refusals: list[Rejection] = []
        self.views: list[MatchView] = []

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        self.views.append(view)
        if rejection is None:
            return ChooseRowAction(row_index=99)
        self.refusals.append(rejection)
        return super().act(view, rejection)


class IllegalBot:
    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        return ChooseRowAction(row_index=99)


class RaisingBot:
    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        msg = "provider unavailable"
        raise RuntimeError(msg)


class MessagingBot:
    def __init__(self) -> None:
        self.calls = 0

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
        self.calls += 1
        return SendMessageAction(visibility="table", body="still thinking")


@pytest.mark.parametrize("communication", [False, True])
def test_every_offer_can_recover_from_a_private_rejection(short_protocol: MatchProtocol, communication: bool) -> None:
    bots = [InvalidThenLegal(1), InvalidThenLegal(2)]
    result = run_match(
        bots,
        123,
        protocol=short_protocol.model_copy(update={"communication_enabled": communication}),
        config=RunConfig(decision_rejection_limit=2),
    )
    assert result.outcome == MatchOutcome.FINISHED
    assert result.actions_accepted == result.actions_rejected
    for index, bot in enumerate(bots):
        assert bot.refusals
        for refusal, view in zip(bot.refusals, bot.views[1::2], strict=True):
            assert refusal.legal_actions == view.legal_actions
            assert refusal.message
        own = Viewer(ViewRole.PLAYER, f"player_{index + 1}")
        assert all(
            event.audience == f"player:{own.player_id}"
            for event in visible_events(result.events, own)
            if event.type == "action_rejected"
        )


@pytest.mark.parametrize(
    "config, outcome, reason, attempts",
    [
        (
            RunConfig(decision_rejection_limit=3, play_action_limit=3, match_action_limit=3),
            "forfeited",
            "wrong_phase",
            3,
        ),
        (
            RunConfig(decision_rejection_limit=8, play_action_limit=3, match_action_limit=3),
            "abandoned",
            "play_action_limit",
            3,
        ),
        (RunConfig(decision_rejection_limit=8, match_action_limit=2), "abandoned", "match_action_limit", 2),
    ],
)
def test_limit_precedence_counts_rejected_attempts(config: RunConfig, outcome: str, reason: str, attempts: int) -> None:
    result = run_match([IllegalBot(), RandomBot(2)], 123, config=config)
    assert result.outcome == outcome
    assert result.reason == reason
    assert result.actions_rejected == attempts
    assert result.winners == ()
    assert result.events[-1].type == "match_abandoned"
    assert result.events[-1].data == {}
    assert_replayed_state(result.events, result.final_state)


def test_row_choice_has_its_own_rejection_budget(short_protocol: MatchProtocol) -> None:
    result = run_match(
        [BadRows(1), BadRows(2)], 123, protocol=short_protocol, config=RunConfig(decision_rejection_limit=3)
    )
    assert result.outcome == MatchOutcome.FORFEITED
    assert result.reason == "invalid_row_index"
    assert result.actions_rejected == 3


def test_finished_match_wins_over_action_limit(short_protocol: MatchProtocol) -> None:
    control = run_match([RandomBot(1), RandomBot(2)], 123, protocol=short_protocol)
    result = run_match(
        [RandomBot(1), RandomBot(2)], 123, protocol=short_protocol, config=RunConfig(match_action_limit=control.actions)
    )
    assert result.outcome == MatchOutcome.FINISHED


def test_play_counter_resets_before_limit_check(short_protocol: MatchProtocol) -> None:
    control = run_match(
        [RandomBot(1), RandomBot(2)], 123, protocol=short_protocol, config=RunConfig(play_action_limit=3)
    )
    assert control.outcome == MatchOutcome.FINISHED


def test_round_robin_offers_every_noncommitting_seat() -> None:
    bots = [MessagingBot(), MessagingBot(), MessagingBot()]
    result = run_match(
        bots, 123, protocol=MatchProtocol(communication_enabled=True), config=RunConfig(play_action_limit=12)
    )
    assert result.outcome == MatchOutcome.ABANDONED
    assert result.reason == "play_action_limit"
    assert [bot.calls for bot in bots] == [4, 4, 4]


@pytest.mark.parametrize("surface", ["match", "arena"])
def test_sequential_communication_is_rejected_before_play(surface: str) -> None:
    protocol = MatchProtocol(communication_enabled=True)
    config = RunConfig(scheduler="sequential")
    with pytest.raises(ValueError, match="starve"):
        if surface == "match":
            run_match([RandomBot(1), RandomBot(2)], 123, protocol=protocol, config=config)
        else:
            run_arena([PlayerConfig(bot="random")] * 2, 1, 123, protocol=protocol, config=config)


@pytest.mark.parametrize("communication, scheduler, limit", [(False, "sequential", None), (True, "round_robin", 200)])
def test_configuration_defaults_follow_mode(communication: bool, scheduler: str, limit: int | None) -> None:
    config = resolve(RunConfig(concurrency=3), MatchProtocol(communication_enabled=communication))
    assert config.scheduler == scheduler
    assert config.play_action_limit == limit
    assert config.max_abandoned_decisions == 12


@pytest.mark.parametrize("communication", [False, True])
def test_live_folds_and_replay_match_every_transition(short_protocol: MatchProtocol, communication: bool) -> None:
    viewers = [Viewer(role) for role in ViewRole if role != ViewRole.PLAYER]
    viewers += [Viewer(ViewRole.PLAYER, f"player_{i + 1}") for i in range(3)]
    folders = {viewer: ViewFolder(viewer) for viewer in viewers}
    log: list[Event] = []

    def inspect(state: MatchState, events: tuple[Event, ...]) -> None:
        log.extend(events)
        assert_replayed_state(log, state)
        for viewer, folder in folders.items():
            folder.apply(events)
            view = folder.view()
            assert view == build_view(log, viewer)
            if viewer.role != ViewRole.PLAYER:
                continue
            cards = set(view.you.hand) | {card for row in view.rows for card in row.cards}
            cards.update(
                card for pile in [view.you.penalty_cards, *(p.penalty_cards for p in view.players)] for card in pile
            )
            cards.update(card for play in view.revealed_this_hand for card in play)
            if view.you.selection is not None:
                cards.add(view.you.selection)
            assert cards.isdisjoint(state.undealt_remainder)
            for player in state.players:
                if player.player_id != viewer.player_id:
                    assert cards.isdisjoint(player.hand)

    run_match(
        [RandomBot(1), RandomBot(2), LowestFittingCardBot()],
        123,
        observer=inspect,
        protocol=short_protocol.model_copy(update={"communication_enabled": communication}),
    )


def test_trace_records_rejections_and_actual_offered_views(tmp_path: Path, short_protocol: MatchProtocol) -> None:
    recording = InvalidThenLegal(1)
    bots = [recording, RandomBot(2)]
    directory = tmp_path / "trace"
    result = run_match(bots, 123, protocol=short_protocol, config=RunConfig(trace_dir=directory))
    events = read_event_log(directory / "arena_0.jsonl")
    records = read_action_log(directory / "arena_0.actions.jsonl")
    assert_replayed_state(events, result.final_state)
    assert [record.server_action_seq for record in records] == list(range(1, result.actions + 1))
    own_records = [record for record in records if record.player_id == "player_1"]
    assert [record.from_view for record in own_records] == [view.view_id for view in recording.views]
    assert sum(record.outcome == "rejected" for record in records) == result.actions_rejected
    assert all(record.action_id == f"arena_0:{record.server_action_seq}" for record in records)
    assert all(record.decision_duration_ms is not None and record.decision_duration_ms >= 0 for record in records)


def test_concurrency_preserves_results_and_event_logs(tmp_path: Path, short_protocol: MatchProtocol) -> None:
    directories = [tmp_path / "one", tmp_path / "eight"]
    results = [
        run_arena(
            [PlayerConfig(bot="random"), PlayerConfig(bot="lowest_fitting_card")],
            8,
            123,
            protocol=short_protocol,
            config=RunConfig(trace_dir=directory, concurrency=concurrency),
        )
        for directory, concurrency in zip(directories, [1, 8], strict=True)
    ]
    assert results[0] == results[1]
    manifests = [json.loads((directory / "manifest.json").read_text()) for directory in directories]
    for first, second in zip(manifests[0]["matches"], manifests[1]["matches"], strict=True):
        logs = [
            read_event_log(directory / entry["log"])
            for directory, entry in zip(directories, [first, second], strict=True)
        ]
        assert [event.model_dump(exclude={"timestamp"}) for event in logs[0]] == [
            event.model_dump(exclude={"timestamp"}) for event in logs[1]
        ]


@pytest.mark.parametrize("outcome, bot", [("forfeited", IllegalBot), ("failed", RaisingBot)])
def test_nonfinished_matches_do_not_contribute_scores(monkeypatch: pytest.MonkeyPatch, outcome: str, bot: Any) -> None:
    monkeypatch.setitem(REGISTRY, "broken", BotSpec("broken", lambda seed: bot(), False, {"provider": "test"}))
    result = run_arena([PlayerConfig(bot="broken"), PlayerConfig(bot="random")], 3, 123)
    assert getattr(result, outcome) == 3
    assert result.games_started == result.games_completed == 3
    assert not result.reproducible
    assert all(player.total_score == player.wins == player.ties == 0 for player in result.players)


def test_first_build_failure_stops_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(REGISTRY, "bad", BotSpec("bad", _fail_bot_build, False, {}))
    with pytest.raises(ArenaError, match="first game's lineup"):
        run_arena([PlayerConfig(bot="bad"), PlayerConfig(bot="random")], 3, 123)


def test_later_build_failure_is_a_match_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, short_protocol: MatchProtocol
) -> None:
    builds = 0

    def build(seed: int) -> RandomBot:
        nonlocal builds
        builds += 1
        if builds == 2:
            msg = "credential refresh failed"
            raise RuntimeError(msg)
        return RandomBot(seed)

    monkeypatch.setitem(REGISTRY, "transient", BotSpec("transient", build, False, {}))
    directory = tmp_path / "trace"
    result = run_arena(
        [PlayerConfig(bot="transient"), PlayerConfig(bot="random")],
        3,
        123,
        protocol=short_protocol,
        config=RunConfig(trace_dir=directory),
    )
    assert result.finished == 2
    assert result.failed == 1
    manifest = json.loads((directory / "manifest.json").read_text())
    entry = manifest["matches"][1]
    assert entry["ended_by"] == "player_1"
    assert "credential refresh" in entry["reason"]
    assert read_event_log(directory / entry["log"])[-1].type == "match_abandoned"


@pytest.mark.parametrize("raises", [False, True])
def test_stats_are_opaque_and_cannot_cost_a_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, short_protocol: MatchProtocol, raises: bool
) -> None:
    class Reporting(RandomBot):
        def stats(self) -> dict[str, Any]:
            if raises:
                msg = "metrics unavailable"
                raise RuntimeError(msg)
            return {"tokens": {"input": 19}, "custom": ["opaque", 2.5]}

    monkeypatch.setitem(REGISTRY, "reporting", BotSpec("reporting", Reporting, True, {"strategy": "test"}))
    directory = tmp_path / "trace"
    result = run_arena(
        [PlayerConfig(bot="reporting"), PlayerConfig(bot="random")],
        1,
        123,
        protocol=short_protocol,
        config=RunConfig(trace_dir=directory),
    )
    assert result.finished == 1
    manifest = json.loads((directory / "manifest.json").read_text())
    entry = manifest["matches"][0]
    assert entry["seat_stats"]["player_2"] is None
    if raises:
        assert entry["seat_stats"]["player_1"] is None
        assert "metrics unavailable" in entry["stats_errors"]["player_1"]
    else:
        assert entry["seat_stats"]["player_1"] == {"tokens": {"input": 19}, "custom": ["opaque", 2.5]}
    assert manifest["seats"][0]["agent_metadata"] == {"strategy": "test", "bot_options": {}}


def test_timeout_is_finalised_while_bot_is_still_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    release = threading.Event()
    returned = threading.Event()

    class Blocked:
        def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
            release.wait()
            returned.set()
            return SelectCardAction(card=view.you.hand[0])

        def stats(self) -> dict[str, Any]:
            pytest.fail("must not enter a bot whose decision is still running")

    monkeypatch.setitem(REGISTRY, "blocked", BotSpec("blocked", lambda seed: Blocked(), False, {}))
    directory = tmp_path / "trace"
    try:
        result = run_arena(
            [PlayerConfig(bot="blocked"), PlayerConfig(bot="random")],
            2,
            123,
            config=RunConfig(trace_dir=directory, decision_timeout_seconds=0.02),
        )
        assert result.failed == 2
        assert result.decisions_abandoned == 2
        assert not returned.is_set()
        manifest = json.loads((directory / "manifest.json").read_text())
        for entry in manifest["matches"]:
            records = read_action_log(directory / entry["actions"])
            assert len(records) == 1
            assert records[0].outcome == "timeout"
            assert records[0].type is None
            assert records[0].from_view is not None
            assert records[0].decision_started_at is not None
            assert records[0].decision_ended_at is not None
            assert read_event_log(directory / entry["log"])[-1].type == "match_abandoned"
    finally:
        release.set()


def test_abandoned_decision_bound_stops_submission(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    release = threading.Event()

    class Blocked:
        def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
            release.wait()
            return CommitAction()

    monkeypatch.setitem(REGISTRY, "blocked", BotSpec("blocked", lambda seed: Blocked(), False, {}))
    directory = tmp_path / "trace"
    try:
        with pytest.raises(ArenaError, match="max_abandoned_decisions"):
            run_arena(
                [PlayerConfig(bot="blocked"), PlayerConfig(bot="random")],
                20,
                123,
                config=RunConfig(trace_dir=directory, decision_timeout_seconds=0.01, max_abandoned_decisions=1),
            )
        manifest = json.loads((directory / "manifest.json").read_text())
        assert manifest["games_started"] == manifest["games_completed"] == 2
    finally:
        release.set()


def test_stop_on_failure_drains_already_started_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, short_protocol: MatchProtocol
) -> None:
    together = threading.Barrier(2)
    built = 0
    lock = threading.Lock()

    class Coordinated(RandomBot):
        def __init__(self, seed: int, fail: bool) -> None:
            super().__init__(seed)
            self.fail = fail
            self.first = True

        def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
            if self.first:
                self.first = False
                together.wait(timeout=5)
                if self.fail:
                    msg = "stop this run"
                    raise RuntimeError(msg)
            return super().act(view, rejection)

    def build(seed: int) -> Coordinated:
        nonlocal built
        with lock:
            built += 1
            return Coordinated(seed, fail=built == 1)

    monkeypatch.setitem(REGISTRY, "coordinated", BotSpec("coordinated", build, False, {}))
    result = run_arena(
        [PlayerConfig(bot="coordinated"), PlayerConfig(bot="random")],
        20,
        123,
        protocol=short_protocol,
        config=RunConfig(concurrency=2, stop_on_failure=True, decision_timeout_seconds=2, trace_dir=tmp_path / "trace"),
    )
    assert result.games_started == result.games_completed == 2
    assert result.failed == result.finished == 1


def test_no_deadline_calls_bot_inline(short_protocol: MatchProtocol) -> None:
    caller = threading.get_ident()
    observed: list[int] = []

    class ThreadAware(RandomBot):
        def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
            observed.append(threading.get_ident())
            return super().act(view, rejection)

    run_match([ThreadAware(1), RandomBot(2)], 123, protocol=short_protocol)
    assert set(observed) == {caller}


def test_existing_trace_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileExistsError):
        run_arena([PlayerConfig(bot="random")] * 2, 1, 123, config=RunConfig(trace_dir=tmp_path))


def test_summary_distinguishes_forfeit_only_with_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(REGISTRY, "illegal", BotSpec("illegal", lambda seed: IllegalBot(), True, {}))
    directory = tmp_path / "trace"
    run_arena([PlayerConfig(bot="illegal"), PlayerConfig(bot="random")], 1, 123, config=RunConfig(trace_dir=directory))
    manifest = json.loads((directory / "manifest.json").read_text())
    entry = ManifestMatch.model_validate(manifest["matches"][0])
    events, actions = read_event_log(directory / entry.log), read_action_log(directory / entry.actions)
    informed = summarise(events, actions, entry)
    uninformed = summarise(events, actions)
    assert informed.outcome == "forfeited"
    assert informed.ended_by == "player_1"
    assert uninformed.outcome == "abandoned"
    assert "No manifest" in uninformed.notes[0]
    assert informed.players[0].actions_rejected == 8
    assert informed.players[0].decision_latencies_ms is not None
    with pytest.raises(ValueError, match="unsupported manifest version"):
        summarise(events, actions, entry.model_copy(update={"manifest_version": 2}))


def assert_replayed_state(events: list[Event] | tuple[Event, ...], state: MatchState) -> None:
    replayed = replay_events(events).state
    # Unused cards are a set: their shuffled order is deliberately not logged.
    assert sorted(replayed.undealt_remainder) == sorted(state.undealt_remainder)
    assert replayed.model_dump(exclude={"undealt_remainder"}) == state.model_dump(exclude={"undealt_remainder"})


def test_non_action_return_is_a_recorded_bot_failure(tmp_path: Path) -> None:
    directory = tmp_path / "trace"
    result = run_match([Malformed(), RandomBot(2)], 123, config=RunConfig(trace_dir=directory))
    assert result.outcome == MatchOutcome.FAILED
    assert result.ended_by == "player_1"
    assert result.reason is not None and "bot returned dict" in result.reason
    record = read_action_log(directory / "arena_0.actions.jsonl")[0]
    assert record.outcome == "error"
    assert record.type is None
    assert result.actions == 0


def test_engine_action_budget_is_distinct_from_harness_limit() -> None:
    result = run_match(
        [MessagingBot(), MessagingBot()],
        123,
        protocol=MatchProtocol(communication_enabled=True, max_actions_per_play=1),
        config=RunConfig(play_action_limit=100, decision_rejection_limit=2),
    )
    assert result.actions_accepted == 2
    assert result.outcome == MatchOutcome.FORFEITED
    assert result.reason == "action_budget_exhausted"


def test_trace_write_failure_stops_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Sabotage(RandomBot):
        def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
            # The sink stages each append beside its log; a directory there
            # creates a real, deterministic filesystem failure.
            for path in directory.glob("*.jsonl"):
                path.with_name(path.name + ".pending").mkdir(exist_ok=True)
            return super().act(view, rejection)

    directory = tmp_path / "trace"
    monkeypatch.setitem(REGISTRY, "sabotage", BotSpec("sabotage", Sabotage, True, {}))
    with pytest.raises(ArenaError):
        run_arena(
            [PlayerConfig(bot="sabotage"), PlayerConfig(bot="random")], 2, 123, config=RunConfig(trace_dir=directory)
        )


def test_scheduler_failure_stops_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SequentialScheduler, "next_seat", _fail_scheduler)
    with pytest.raises(ArenaError, match="scheduler defect"):
        run_arena([PlayerConfig(bot="random")] * 2, 3, 123)


def test_lowest_fitting_card_chooses_lowest_cost_card_and_lowest_row_on_ties() -> None:
    from sixnimmt.engine.setup import create_match
    from sixnimmt.engine.views import RowView

    _, events = create_match("lowest_fitting_card", ["a", "b"], 123)
    view = build_view(events, Viewer(ViewRole.PLAYER, "a"))
    rows = tuple(
        RowView(index=index, cards=cards) for index, cards in enumerate([(10,), (20, 21, 22, 23, 24), (60,), (90,)])
    )
    view = view.model_copy(update={"rows": rows, "you": view.you.model_copy(update={"hand": (1, 25, 61, 62)})})
    assert LowestFittingCardBot().act(view) == SelectCardAction(card=61)
    view = view.model_copy(update={"legal_actions": ("choose_row",)})
    assert LowestFittingCardBot().act(view) == ChooseRowAction(row_index=0)


def test_round_robin_resumes_and_skips_committed_seats() -> None:
    from sixnimmt.arena.scheduling import RoundRobinScheduler
    from sixnimmt.engine.setup import create_match

    state, _ = create_match("schedule", ["a", "b", "c"], 123)
    scheduler = RoundRobinScheduler()
    assert scheduler.next_seat(state) == 0
    state = state.model_copy(
        update={
            "players": (state.players[0], state.players[1].model_copy(update={"committed": True}), state.players[2])
        }
    )
    assert scheduler.next_seat(state) == 2
    assert scheduler.next_seat(state) == 0
    state = state.model_copy(
        update={"players": tuple(player.model_copy(update={"committed": False}) for player in state.players)}
    )
    assert scheduler.next_seat(state) == 1


def test_hidden_direct_messages_do_not_change_uninvolved_bot_view(short_protocol: MatchProtocol) -> None:
    from sixnimmt.engine.rules import InformationPolicy

    protocol = short_protocol.model_copy(
        update={
            "communication_enabled": True,
            "information_policy": InformationPolicy(private_message_existence="hidden"),
        }
    )
    views: list[MatchView] = []

    class Observed(RandomBot):
        def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
            views.append(view)
            return super().act(view, rejection)

    folder = ViewFolder(Viewer(ViewRole.PLAYER, "player_3"))

    def inspect(state: MatchState, events: tuple[Event, ...]) -> None:
        before = folder.view()
        folder.apply(events)
        if any(event.type == "message_sent" for event in events):
            assert folder.view().model_dump_json() == before.model_dump_json()

    result = run_match([HiddenSender(1), RandomBot(2), Observed(3)], 123, protocol=protocol, observer=inspect)
    assert result.outcome == MatchOutcome.FINISHED
    assert views
    assert all(view.messages == () and view.private_messages_observed == () for view in views)


def test_live_folder_applies_each_visible_event_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import sixnimmt.engine.fold as fold
    from sixnimmt.engine.rules import GameRules
    from sixnimmt.engine.setup import create_match
    from sixnimmt.engine.transition import transition

    protocol = MatchProtocol(communication_enabled=True)
    state, initial = create_match("messages", ["a", "b"], 123, protocol=protocol)
    applied = 0
    original = fold._apply

    def count(state: Any, event: Event, viewer: Viewer) -> None:
        nonlocal applied
        applied += 1
        original(state, event, viewer)

    monkeypatch.setattr(fold, "_apply", count)
    viewer = Viewer(ViewRole.PLAYER, "a")
    folder = ViewFolder(viewer)
    folder.apply(initial)
    expected = len(visible_events(initial, viewer))
    for _ in range(1000):
        state, batch = transition(
            state, "a", SendMessageAction(visibility="table", body="hello"), protocol, GameRules()
        )
        folder.apply(batch)
        folder.view()
        expected += len(visible_events(batch, viewer))
    assert applied == expected


def test_concurrent_matches_have_distinct_instances_and_bounded_capacity(
    monkeypatch: pytest.MonkeyPatch, short_protocol: MatchProtocol
) -> None:
    together = threading.Barrier(3)
    lock = threading.Lock()
    active = 0
    maximum = 0
    instances: list[RandomBot] = []

    class Isolated(RandomBot):
        def __init__(self, seed: int) -> None:
            super().__init__(seed)
            self.match_id: str | None = None
            self.first = True

        def act(self, view: MatchView, rejection: Rejection | None = None) -> Action:
            nonlocal active, maximum
            if self.match_id is None:
                self.match_id = view.match_id
            assert self.match_id == view.match_id
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                if self.first:
                    self.first = False
                    together.wait(timeout=5)
                return super().act(view, rejection)
            finally:
                with lock:
                    active -= 1

    def build(seed: int) -> Isolated:
        instance = Isolated(seed)
        with lock:
            instances.append(instance)
        return instance

    monkeypatch.setitem(REGISTRY, "isolated", BotSpec("isolated", build, True, {}))
    result = run_arena(
        [PlayerConfig(bot="isolated"), PlayerConfig(bot="random")],
        6,
        123,
        protocol=short_protocol,
        config=RunConfig(concurrency=3),
    )
    assert result.finished == 6
    assert len(instances) == len({id(bot) for bot in instances}) == 6
    assert maximum == 3


def test_direct_match_trace_has_manifest(tmp_path: Path, short_protocol: MatchProtocol) -> None:
    directory = tmp_path / "trace"
    run_match([RandomBot(1), RandomBot(2)], 123, protocol=short_protocol, config=RunConfig(trace_dir=directory))
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["matches"][0]["outcome"] == "finished"
    assert manifest["games_started"] == manifest["games_completed"] == 1
    assert manifest["reproducible"] is False  # Direct instances have no registry declaration.


def test_legacy_fold_keeps_selection_only_action_counts() -> None:
    from sixnimmt.engine.rules import GameRules
    from sixnimmt.engine.setup import create_match
    from sixnimmt.engine.transition import transition

    state, events = create_match("legacy", ["a", "b"], 123)
    _, selected = transition(state, "a", SelectCardAction(card=state.players[0].hand[0]), MatchProtocol(), GameRules())
    legacy = [event for event in [*events, *selected] if event.type != "action_counted"]
    view = build_view(legacy, Viewer(ViewRole.PLAYER, "a"))
    assert view.you.actions_taken_this_play == 1
    assert view.you.committed


def test_failure_reason_is_written_before_manifest(tmp_path: Path) -> None:
    from sixnimmt.persistence.sink import JsonlEventSink

    sink = JsonlEventSink(tmp_path, "failure")
    try:
        result = run_match([RaisingBot(), RandomBot(2)], 123, match_id="failure", sink=sink)
        assert not (tmp_path / "manifest.json").exists()
        events = read_event_log(tmp_path / "failure.jsonl")
        failure = next(event for event in events if event.type == "match_abandoned" and event.audience == "admin")
        assert failure.data == {"outcome": "failed", "ended_by": "player_1", "reason": result.reason}
        assert "provider unavailable" in failure.data["reason"]
        assert events[-1].audience == "public"
        assert "reason" not in events[-1].data
        assert read_action_log(tmp_path / "failure.actions.jsonl")[0].reason == result.reason
    finally:
        sink.close()
