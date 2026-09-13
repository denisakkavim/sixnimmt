"""Public match API and tournament orchestration."""

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime

from sixnimmt.arena.aggregation import _Aggregate
from sixnimmt.arena.bots.base import Bot
from sixnimmt.arena.config import RunConfig, resolve, resolved_abandoned_limit
from sixnimmt.arena.decisions import AbandonedDecisions, SharedAbandonedState
from sixnimmt.arena.execution import _drive_games, _GameJob, _validate_process_players
from sixnimmt.arena.execution import derive_seed as derive_seed
from sixnimmt.arena.match import ArenaError as ArenaError
from sixnimmt.arena.match import Observer as Observer
from sixnimmt.arena.match import _validate_player_count
from sixnimmt.arena.match import run_match as run_match
from sixnimmt.arena.players import PlayerConfig, resolve_players
from sixnimmt.arena.results import ArenaResult
from sixnimmt.arena.results import MatchOutcome as MatchOutcome
from sixnimmt.arena.results import MatchResult as MatchResult
from sixnimmt.arena.tracing import write_arena_manifest
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.state import PlayerSeat
from sixnimmt.persistence.manifest import ManifestMatch

DEFAULT_MAX_ACTIONS = 10_000


def run_arena(
    players: Sequence[PlayerConfig],
    games: int,
    seed: int,
    *,
    rules: GameRules | None = None,
    protocol: MatchProtocol | None = None,
    config: RunConfig | None = None,
    max_actions_per_match: int | None = None,
    observer: Observer | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> ArenaResult:
    """Run fresh seats per match, retaining full provenance only when tracing.

    on_progress receives the completed match count in the calling thread after
    each result, for either backend. Callback exceptions propagate to the caller.
    """
    _validate_player_count(len(players))
    if games < 1:
        msg = "games must be positive"
        raise ValueError(msg)
    rules = GameRules() if rules is None else rules
    protocol = MatchProtocol() if protocol is None else protocol
    config = RunConfig() if config is None else config
    if max_actions_per_match is not None:
        config = replace(config, match_action_limit=max_actions_per_match)
    config = resolve(config, protocol)
    specs = resolve_players(players)
    if not rules.min_players <= len(players) <= rules.max_players:
        msg = "player count is outside the configured game rules"
        raise ValueError(msg)
    seats = [
        PlayerSeat(
            player_id=f"player_{i + 1}",
            display_name=spec.config.display_name if spec.config.display_name is not None else f"Player {i + 1}",
            agent_metadata=spec.metadata,
        )
        for i, spec in enumerate(specs)
    ]
    initial_bots: list[Bot] = []
    if config.backend == "process":
        _validate_process_players(specs, observer)
    else:
        # Thread mode retains its eager first-lineup validation. Process mode
        # constructs every bot in its worker, including the first lineup.
        try:
            initial_bots = [spec.build(derive_seed(seed, "bot", 0, i)) for i, spec in enumerate(specs)]
        except Exception as error:
            msg = f"cannot construct first game's lineup: {error!r}"
            raise ArenaError(msg) from error
    run_id = f"arena_{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{seed}"
    if config.trace_dir is not None:
        config.trace_dir.mkdir(parents=True, exist_ok=False)
    shared = None
    if config.backend == "process" and config.decision_timeout_seconds is not None:
        shared = SharedAbandonedState.create()
    abandoned = AbandonedDecisions(resolved_abandoned_limit(config), shared)
    aggregate = _Aggregate([spec.name for spec in specs], [seat.display_name for seat in seats])
    entries: list[ManifestMatch] = []
    started, fatal = _drive_games(
        games,
        _GameJob(seed, specs, seats, rules, protocol, config, run_id),
        abandoned,
        observer,
        initial_bots,
        aggregate,
        entries,
        on_progress,
    )
    result = aggregate.result(run_id, seed, games, started, abandoned.total, all(spec.deterministic for spec in specs))
    write_arena_manifest(result, specs, seats, rules, protocol, config, entries, fatal)
    if fatal is not None:
        msg = f"arena run failed after {result.games_completed}/{started} started matches completed: {fatal}"
        raise ArenaError(msg) from fatal
    return result
