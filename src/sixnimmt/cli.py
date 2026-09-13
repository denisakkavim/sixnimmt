"""Command-line interface for strategy comparisons and match inspection."""

import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from sixnimmt.analytics.summary import summarise as summarise_match
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.match import ArenaError
from sixnimmt.arena_cli import ComparisonOptions, run_comparison
from sixnimmt.engine.replay import ReplayedMatch, replay_events
from sixnimmt.persistence.manifest import ManifestMatch
from sixnimmt.persistence.sink import read_action_log, read_event_log

app = typer.Typer(help="Run deterministic 6 nimmt! tools.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Run deterministic 6 nimmt! tools."""


@app.command()
def arena(
    ctx: typer.Context,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="JSON strategy and arena settings; defaults to the reference strategies."),
    ] = None,
    games: Annotated[
        int | None, typer.Option("--games", min=0, help="Random-opponent games per player count; default 100.")
    ] = None,
    player_count: Annotated[
        list[int] | None,
        typer.Option(
            "--player-count", min=2, max=10, help="Players per game; default 4. Repeat to compare table sizes."
        ),
    ] = None,
    controlled_games: Annotated[
        int | None,
        typer.Option("--controlled-games", min=0, help="Additional games per selected opponent lineup; default 0."),
    ] = None,
    seed: Annotated[int, typer.Option("--seed", help="Seed for reproducible games.")] = 66,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", help="Save results and reports in this new directory; omit to run in memory."),
    ] = None,
    trace: Annotated[
        bool,
        typer.Option("--trace", help="Save detailed game logs under traces/; requires --output-dir."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print the structured report as JSON.")] = False,
    animation: Annotated[
        bool,
        typer.Option("--animation/--no-animation", help="Show a bull-and-card animation in interactive terminals."),
    ] = True,
    communication: Annotated[
        bool, typer.Option("--communication", help="Allow messages and card changes before explicit commitment.")
    ] = False,
    concurrency: Annotated[int, typer.Option("--concurrency", min=1, help="Maximum games running at once.")] = 1,
    backend: Annotated[str, typer.Option("--backend", help="Execution backend: thread or process.")] = "thread",
    decision_timeout: Annotated[
        float | None, typer.Option("--decision-timeout", help="Seconds allowed for each bot call.")
    ] = None,
    match_action_limit: Annotated[
        int, typer.Option("--match-action-limit", min=1, help="Maximum attempted actions in a game.")
    ] = 10_000,
    play_action_limit: Annotated[int | None, typer.Option("--play-action-limit", min=1)] = None,
    decision_rejection_limit: Annotated[int, typer.Option("--decision-rejection-limit", min=1)] = 8,
    max_abandoned_decisions: Annotated[int | None, typer.Option("--max-abandoned-decisions", min=0)] = None,
    scheduler: Annotated[str | None, typer.Option("--scheduler")] = None,
    stop_on_failure: Annotated[bool, typer.Option("--stop-on-failure")] = False,
) -> None:
    """Play games across opponent lineups and compare their results."""
    try:
        config = RunConfig(
            scheduler=scheduler,
            concurrency=concurrency,
            backend=backend,
            decision_timeout_seconds=decision_timeout,
            play_action_limit=play_action_limit,
            match_action_limit=match_action_limit,
            decision_rejection_limit=decision_rejection_limit,
            max_abandoned_decisions=max_abandoned_decisions,
            stop_on_failure=stop_on_failure,
        )
        options = ComparisonOptions(
            config_file=config_file,
            player_counts=None if player_count is None else tuple(player_count),
            games=games,
            controlled_games=controlled_games,
            output_dir=output_dir,
            trace=trace,
            json_output=json_output,
            animation=animation,
        )
        run_comparison(ctx, options, config, seed=seed, communication=communication)
    except ValueError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    except (ArenaError, OSError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error


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
