"""Command-line interface: deterministic arenas, the HTTP server, and replay."""

import json
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from pydantic import TypeAdapter, ValidationError

from sixnimmt_server.analytics.summary import summarise as summarise_match
from sixnimmt_server.arena.players import PlayerConfig
from sixnimmt_server.arena.runner import ArenaError, ArenaResult, RunConfig, run_arena
from sixnimmt_server.engine.replay import ReplayedMatch, replay_events
from sixnimmt_server.engine.rules import MatchProtocol
from sixnimmt_server.persistence.manifest import ManifestMatch
from sixnimmt_server.persistence.sink import read_action_log, read_event_log
from sixnimmt_server.server.app import create_app
from sixnimmt_server.server.auth import mint_token

app = typer.Typer(help="Run deterministic 6 nimmt! tools.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Run deterministic 6 nimmt! tools."""


def _print_result(result: ArenaResult) -> None:
    typer.echo(f"seed={result.seed}")
    typer.echo(f"games={result.games}")
    typer.echo(f"total_hands={result.total_hands}")
    typer.echo(f"total_actions={result.total_actions}")
    typer.echo(f"requested={result.games_requested} started={result.games_started} completed={result.games_completed}")
    typer.echo(
        f"finished={result.finished} abandoned={result.abandoned} forfeited={result.forfeited} failed={result.failed}"
    )
    for player in result.players:
        average_score = player.total_score / result.finished if result.finished else 0.0
        typer.echo(
            f"{player.player_id} bot={player.bot_name} wins={player.wins} "
            f"ties={player.ties} total_score={player.total_score} "
            f"average_score={average_score:.2f} display_name={player.display_name}"
        )


@app.command()
def arena(
    players_file: Annotated[
        Path,
        typer.Option(
            "--players-file", help="JSON array of per-player bot, display_name, options, and agent_metadata settings."
        ),
    ],
    games: Annotated[int, typer.Option("--games", help="Number of matches to run.")],
    seed: Annotated[int, typer.Option("--seed", help="Root seed for deterministic matches.")],
    max_actions_per_match: Annotated[
        int,
        typer.Option("--max-actions-per-match", help="Maximum bot actions allowed in each match."),
    ] = 10_000,
    negotiation: Annotated[bool, typer.Option("--negotiation")] = False,
    scheduler: Annotated[str | None, typer.Option("--scheduler")] = None,
    trace_dir: Annotated[
        Path | None,
        typer.Option(
            "--trace-dir", help="New directory for experiment traces; without it, only smoke-test aggregates are kept."
        ),
    ] = None,
    concurrency: Annotated[int, typer.Option("--concurrency")] = 1,
    decision_timeout: Annotated[
        float | None,
        typer.Option("--decision-timeout", help="Seconds per bot call; model bots should also set client timeouts."),
    ] = None,
    play_action_limit: Annotated[int | None, typer.Option("--play-action-limit")] = None,
    match_action_limit: Annotated[int | None, typer.Option("--match-action-limit")] = None,
    decision_rejection_limit: Annotated[int, typer.Option("--decision-rejection-limit")] = 8,
    max_abandoned_decisions: Annotated[int | None, typer.Option("--max-abandoned-decisions")] = None,
    stop_on_failure: Annotated[bool, typer.Option("--stop-on-failure")] = False,
) -> None:
    """Run an in-process arena and print outcomes and finished-match scores."""
    if stop_on_failure and decision_timeout is None:
        typer.echo(
            "Warning: --stop-on-failure without --decision-timeout can wait indefinitely for an in-flight bot.",
            err=True,
        )
    try:
        result = run_arena(
            _read_players(players_file),
            games,
            seed,
            protocol=MatchProtocol(negotiation_enabled=negotiation),
            config=RunConfig(
                scheduler=scheduler,
                trace_dir=trace_dir,
                concurrency=concurrency,
                decision_timeout_seconds=decision_timeout,
                play_action_limit=play_action_limit,
                match_action_limit=match_action_limit if match_action_limit is not None else max_actions_per_match,
                decision_rejection_limit=decision_rejection_limit,
                max_abandoned_decisions=max_abandoned_decisions,
                stop_on_failure=stop_on_failure,
            ),
        )
    except ValueError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    except (ArenaError, OSError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error

    _print_result(result)


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8000,
    admin_token: Annotated[
        str | None,
        typer.Option("--admin-token", help="Admin bearer token. Generated and printed when omitted."),
    ] = None,
    log_dir: Annotated[
        Path,
        typer.Option("--log-dir", help="Directory for per-match JSONL logs."),
    ] = Path("logs"),
) -> None:
    """Run the HTTP game server."""
    token = admin_token or mint_token()
    if admin_token is None:
        # Match creation needs this before any match exists, so it cannot be
        # minted per match and has to be told to the operator once.
        typer.echo(f"admin token: {token}")
    typer.echo(f"match logs: {log_dir}")
    uvicorn.run(create_app(admin_token=token, log_directory=log_dir), host=host, port=port)


def _print_replay(replayed: ReplayedMatch, events: int) -> None:
    state = replayed.state
    typer.echo(f"match={state.match_id}")
    typer.echo(f"status={replayed.status}")
    typer.echo(f"events={events}")
    typer.echo(f"hands={state.hand_number}")
    typer.echo(f"plays={state.play_number}")
    for player in state.players:
        typer.echo(
            f"{player.player_id} total_score={player.total_score} "
            f"score_this_hand={player.score_this_hand} cards_in_hand={len(player.hand)}"
        )
    typer.echo(f"winners={','.join(replayed.winners)}")


@app.command()
def replay(
    path: Annotated[Path, typer.Argument(help="A match log written by the server, logs/{match_id}.jsonl.")],
) -> None:
    """Fold a match log back into its final state and print the result."""
    if not path.is_file():
        typer.echo(f"Error: no match log at {path}", err=True)
        raise typer.Exit(code=2)
    try:
        events = read_event_log(path)
    except ValidationError as error:
        typer.echo(f"Error: {path} is not a match log: {error.error_count()} unreadable entries", err=True)
        raise typer.Exit(code=2) from error

    _print_replay(replay_events(events), len(events))


@app.command()
def summarise(
    path: Annotated[Path, typer.Argument(help="A match event log from either surface.")],
    manifest: Annotated[
        Path | None, typer.Option("--manifest", help="Run manifest; defaults to manifest.json beside the log.")
    ] = None,
) -> None:
    """Print scores, actions, messages and measured decision latencies as JSON."""
    try:
        events = read_event_log(path)
        actions = read_action_log(path.with_name(path.stem + ".actions.jsonl"))
        manifest_path = manifest if manifest is not None else path.parent / "manifest.json"
        entry = None
        if manifest is not None or manifest_path.exists():
            entry = _manifest_entry(manifest_path, path.name)
        summary = summarise_match(events, actions, entry)
    except (OSError, ValueError, KeyError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    typer.echo(summary.model_dump_json(indent=2))


def _manifest_entry(path: Path, log_name: str) -> ManifestMatch:
    contents = json.loads(path.read_text(encoding="utf-8"))
    if contents.get("manifest_version") != 1:
        msg = "unsupported manifest version"
        raise ValueError(msg)
    entries = [item for item in contents["matches"] if item["log"] == log_name]
    if len(entries) != 1:
        msg = "manifest must contain exactly one entry for this log"
        raise ValueError(msg)
    return ManifestMatch.model_validate(entries[0])


def _read_players(path: Path) -> list[PlayerConfig]:
    """Parse the same structured seat configuration accepted by Python callers."""
    try:
        return TypeAdapter(list[PlayerConfig]).validate_json(path.read_text(encoding="utf-8"))
    except OSError as error:
        msg = f"cannot read players file {path}: {error}"
        raise ValueError(msg) from error
