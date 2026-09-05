"""Command-line interface: deterministic arenas, the HTTP server, and replay."""

from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from pydantic import ValidationError
from typer._click.core import Context
from typer.core import TyperCommand

from sixnimmt_server.arena.runner import ArenaError, ArenaResult, run_arena
from sixnimmt_server.engine.replay import ReplayedMatch, replay_events
from sixnimmt_server.server.app import create_app
from sixnimmt_server.server.auth import mint_token
from sixnimmt_server.server.sink import read_event_log


class PlayersCommand(TyperCommand):
    """Accept either repeated or contiguous values for ``--players``."""

    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        normalized_args: list[str] = []
        index = 0

        while index < len(args):
            argument = args[index]
            normalized_args.append(argument)
            index += 1

            if argument != "--players":
                continue
            if index >= len(args) or args[index].startswith("-"):
                ctx.fail("Option '--players' requires an argument.")

            normalized_args.append(args[index])
            index += 1
            while index < len(args) and not args[index].startswith("-"):
                normalized_args.extend(("--players", args[index]))
                index += 1

        return super().parse_args(ctx, normalized_args)


app = typer.Typer(help="Run deterministic 6 nimmt! tools.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Run deterministic 6 nimmt! tools."""


def _print_result(result: ArenaResult) -> None:
    typer.echo(f"seed={result.seed}")
    typer.echo(f"games={result.games}")
    typer.echo(f"total_hands={result.total_hands}")
    typer.echo(f"total_actions={result.total_actions}")
    for player in result.players:
        average_score = player.total_score / result.games
        typer.echo(
            f"{player.player_id} bot={player.bot_name} wins={player.wins} "
            f"ties={player.ties} total_score={player.total_score} "
            f"average_score={average_score:.2f}"
        )


@app.command(cls=PlayersCommand)
def arena(
    players: Annotated[
        list[str],
        typer.Option("--players", help="Bot names, either repeated or space-separated."),
    ],
    games: Annotated[int, typer.Option("--games", help="Number of matches to run.")],
    seed: Annotated[int, typer.Option("--seed", help="Root seed for deterministic matches.")],
    max_actions_per_match: Annotated[
        int,
        typer.Option("--max-actions-per-match", help="Maximum bot actions allowed in each match."),
    ] = 10_000,
) -> None:
    """Run an in-process arena and print aggregate results."""
    try:
        result = run_arena(
            players,
            games,
            seed,
            max_actions_per_match=max_actions_per_match,
        )
    except ValueError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    except ArenaError as error:
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
