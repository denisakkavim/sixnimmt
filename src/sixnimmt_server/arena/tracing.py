"""Bot statistics and provenance for directly constructed match lineups."""

import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from sixnimmt_server.arena.bots import Bot
from sixnimmt_server.arena.config import RunConfig
from sixnimmt_server.arena.results import MatchResult
from sixnimmt_server.engine.rules import GameRules, MatchProtocol
from sixnimmt_server.engine.state import PlayerSeat
from sixnimmt_server.persistence.manifest import ManifestMatch, write_manifest


def collect_stats(
    bots: Sequence[Bot], timed_out_seat: str | None, player_ids: Sequence[str] | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    stats: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for index, bot in enumerate(bots):
        player_id = player_ids[index] if player_ids is not None else f"player_{index + 1}"
        stats[player_id] = None
        if player_id == timed_out_seat:
            # The abandoned call may still mutate the bot or hold its locks.
            errors[player_id] = "statistics unavailable while a timed-out decision may still be running"
            continue
        report = getattr(bot, "stats", None)
        if report is None:
            continue
        try:
            value = report()
            json.dumps(value, allow_nan=False)
            stats[player_id] = deepcopy(value)
        except Exception as error:
            errors[player_id] = repr(error)
    return stats, errors


def write_standalone_manifest(
    result: MatchResult,
    bots: Sequence[Bot],
    seats: Sequence[PlayerSeat],
    rules: GameRules,
    protocol: MatchProtocol,
    config: RunConfig,
    abandoned: int,
) -> None:
    if config.trace_dir is None:
        return
    stats, errors = collect_stats(
        bots, result.ended_by if result.reason == "decision_timeout" else None, [seat.player_id for seat in seats]
    )
    match_id = result.final_state.match_id
    entry = ManifestMatch(
        game_index=0,
        match_id=match_id,
        seed=result.seed,
        outcome=result.outcome.value,
        winners=result.winners,
        ended_by=result.ended_by,
        reason=result.reason,
        log=f"{match_id}.jsonl",
        actions=f"{match_id}.actions.jsonl",
        seat_stats=stats,
        stats_errors=errors,
    )
    # Direct bot instances carry no reproducibility declaration. Do not infer
    # one from a class name; registered runs supply that declaration explicitly.
    manifest = {
        "manifest_version": 1,
        "run_id": match_id,
        "seed": result.seed,
        "games_requested": 1,
        "games_started": 1,
        "games_completed": 1,
        "reproducible": False,
        "rules": rules.model_dump(mode="json"),
        "protocol": protocol.model_dump(mode="json"),
        "run_config": {key: value for key, value in asdict(config).items() if key != "trace_dir"},
        "seats": [
            {
                "player_id": seat.player_id,
                "bot": type(bot).__qualname__,
                "deterministic": None,
                "agent_metadata": seat.agent_metadata,
            }
            for seat, bot in zip(seats, bots, strict=True)
        ],
        "matches": [entry.model_dump(mode="json")],
        "decisions_abandoned": abandoned,
        "error": None,
    }
    write_manifest(config.trace_dir, manifest)
