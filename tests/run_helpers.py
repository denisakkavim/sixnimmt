"""Small fixed-lineup inputs for tests of the canonical planned runtime."""

from collections.abc import Callable, Sequence
from pathlib import Path

from sixnimmt.arena.artifacts import ArenaRun, MatchRecord
from sixnimmt.arena.catalogue import CandidateConfig, canonical_json
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.execution import run_plan
from sixnimmt.arena.match import Observer
from sixnimmt.arena.planning import ArenaPlan, RunSettings, build_arena_plan
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.engine.rules import GameRules, MatchProtocol


def fixed_plan(
    players: Sequence[PlayerConfig],
    games: int,
    seed: int,
    *,
    rules: GameRules | None = None,
    protocol: MatchProtocol | None = None,
    config: RunConfig | None = None,
) -> ArenaPlan:
    candidates: dict[str, CandidateConfig] = {}
    lineup: list[str] = []
    for player in players:
        if not isinstance(player, PlayerConfig):
            msg = "players must contain PlayerConfig objects"
            raise TypeError(msg)
        identity = canonical_json({"bot": player.bot, "options": player.options})
        if identity not in candidates:
            key = f"strategy_{len(candidates) + 1}"
            candidates[identity] = CandidateConfig(
                bot=player.bot, key=key, label=player.display_name, options=player.options
            )
        key = candidates[identity].key
        assert key is not None
        lineup.append(key)
    settings = RunSettings(
        catalogue=tuple(candidates.values()),
        lineup=tuple(lineup),
        games=games,
        seed=seed,
        rules=GameRules() if rules is None else rules,
        protocol=MatchProtocol() if protocol is None else protocol,
        execution=RunConfig() if config is None else config,
    )
    return build_arena_plan(settings)


def run_fixed(
    players: Sequence[PlayerConfig],
    games: int,
    seed: int,
    *,
    rules: GameRules | None = None,
    protocol: MatchProtocol | None = None,
    config: RunConfig | None = None,
    observer: Observer | None = None,
    on_progress: Callable[[int], None] | None = None,
    output_dir: Path | None = None,
    trace: bool = False,
) -> ArenaRun:
    plan = fixed_plan(players, games, seed, rules=rules, protocol=protocol, config=config)
    return run_plan(plan, output_dir=output_dir, trace=trace, observer=observer, on_progress=on_progress)


def match_facts(record: MatchRecord) -> tuple[object, ...]:
    """Compare game outcomes independently of wall-clock performance and PIDs."""
    return (
        record.job_id,
        record.seats,
        record.outcome,
        record.scores,
        record.winners,
        record.completed_hand_scores,
        record.partial_scores,
        record.ended_by,
        record.reason,
        record.actions_accepted,
        record.actions_rejected,
        record.seat_actions,
    )
