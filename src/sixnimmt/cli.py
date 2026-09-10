"""Command-line interface for deterministic arenas and replay."""

import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import TypeAdapter, ValidationError
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from sixnimmt.analytics.summary import summarise as summarise_match
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.arena.runner import ArenaError, ArenaResult, RunConfig, run_arena
from sixnimmt.engine.replay import ReplayedMatch, replay_events
from sixnimmt.engine.rules import MatchProtocol
from sixnimmt.persistence.manifest import ManifestMatch
from sixnimmt.persistence.sink import read_action_log, read_event_log
from sixnimmt.terminal import arena_animation

app = typer.Typer(help="Run deterministic 6 nimmt! tools.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Run deterministic 6 nimmt! tools."""


def _print_result(result: ArenaResult) -> None:
    console = Console(highlight=False)
    console.print("\n6 nimmt! · Arena results", style="bold cyan")
    console.print(f"Seed {result.seed} · Hands {result.total_hands:,} · Actions {result.total_actions:,}", style="dim")
    console.print(
        f"Matches: {result.games_requested:,} requested · "
        f"{result.games_started:,} started · {result.games_completed:,} completed"
    )

    outcomes = Table(box=box.SIMPLE, padding=(0, 1))
    for label, count, style in (
        ("Finished", result.finished, "green"),
        ("Abandoned", result.abandoned, "yellow"),
        ("Forfeited", result.forfeited, "yellow"),
        ("Failed", result.failed, "red"),
    ):
        outcomes.add_column(label, justify="right", header_style="bold", style=style if count > 0 else "dim")
    outcomes.add_row(*(f"{count:,}" for count in (result.finished, result.abandoned, result.forfeited, result.failed)))
    console.print(outcomes)

    scores = Table(box=box.SIMPLE_HEAD, header_style="bold", padding=(0, 1), leading=1)
    scores.add_column("Player", overflow="fold")
    scores.add_column("Bot", overflow="fold", style="dim")
    for label in ("Wins", "Ties", "Total", "Avg"):
        scores.add_column(label, justify="right")
    for player in result.players:
        name = Text(player.display_name or player.player_id, style="bold")
        name.append(f"\n{player.player_id}", style="dim")
        average_score = f"{player.total_score / result.finished:,.2f}" if result.finished > 0 else "—"
        scores.add_row(
            name,
            Text(player.bot_name),
            f"{player.wins:,}",
            f"{player.ties:,}",
            f"{player.total_score:,}",
            average_score,
        )
    console.print(scores)
    console.print("Scores count finished matches only. Lower is better. Wins exclude ties.", style="dim")
    if result.finished == 0:
        console.print("No finished matches; average scores are unavailable.", style="yellow")
    console.print()


@app.command()
def arena(
    players_file: Annotated[
        Path,
        typer.Option(
            "--players-file", help="JSON array of per-player bot, display_name, options, and agent_metadata settings."
        ),
    ],
    games: Annotated[int, typer.Option("--games", help="Number of matches to run.")],
    seed: Annotated[int, typer.Option("--seed", help="Root seed for deterministic matches.")] = 66,
    max_actions_per_match: Annotated[
        int,
        typer.Option("--max-actions-per-match", help="Maximum bot actions allowed in each match."),
    ] = 10_000,
    communication: Annotated[
        bool,
        typer.Option("--communication", help="Allow optional messages and card changes before explicit commitment."),
    ] = False,
    scheduler: Annotated[str | None, typer.Option("--scheduler")] = None,
    trace_dir: Annotated[
        Path | None,
        typer.Option(
            "--trace-dir", help="New directory for experiment traces; without it, only smoke-test aggregates are kept."
        ),
    ] = None,
    concurrency: Annotated[int, typer.Option("--concurrency")] = 1,
    backend: Annotated[str, typer.Option("--backend", help="Match execution backend: thread or process.")] = "thread",
    decision_timeout: Annotated[
        float | None,
        typer.Option("--decision-timeout", help="Seconds per bot call; model bots should also set client timeouts."),
    ] = None,
    play_action_limit: Annotated[int | None, typer.Option("--play-action-limit")] = None,
    match_action_limit: Annotated[int | None, typer.Option("--match-action-limit")] = None,
    decision_rejection_limit: Annotated[int, typer.Option("--decision-rejection-limit")] = 8,
    max_abandoned_decisions: Annotated[int | None, typer.Option("--max-abandoned-decisions")] = None,
    stop_on_failure: Annotated[bool, typer.Option("--stop-on-failure")] = False,
    animation: Annotated[
        bool,
        typer.Option("--animation/--no-animation", help="Show a bull-and-card animation in interactive terminals."),
    ] = True,
) -> None:
    """Run an arena and print outcomes and finished-match scores."""
    if stop_on_failure and decision_timeout is None:
        typer.echo(
            "Warning: --stop-on-failure without --decision-timeout can wait indefinitely for an in-flight bot.",
            err=True,
        )
    try:
        players = _read_players(players_file)
        config = RunConfig(
            scheduler=scheduler,
            trace_dir=trace_dir,
            concurrency=concurrency,
            backend=backend,
            decision_timeout_seconds=decision_timeout,
            play_action_limit=play_action_limit,
            match_action_limit=match_action_limit if match_action_limit is not None else max_actions_per_match,
            decision_rejection_limit=decision_rejection_limit,
            max_abandoned_decisions=max_abandoned_decisions,
            stop_on_failure=stop_on_failure,
        )
        with arena_animation(games, seed, players, enabled=animation) as display:
            result = run_arena(
                players,
                games,
                seed,
                protocol=MatchProtocol(communication_enabled=communication),
                config=config,
                on_progress=display.update if display is not None else None,
            )
    except ValueError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    except (ArenaError, OSError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error

    _print_result(result)


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
    path: Annotated[Path, typer.Argument(help="A match event log.")],
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
