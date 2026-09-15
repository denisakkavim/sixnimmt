"""One play command for fixed or sampled lineups, plus saved-match inspection."""

import json
from collections.abc import Callable
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from typing import Annotated

import typer
from pydantic import JsonValue, TypeAdapter, ValidationError
from rich.console import Console

from sixnimmt.analytics.summary import summarise as summarise_match
from sixnimmt.analytics.terminal import terminal_report
from sixnimmt.application import RunResult
from sixnimmt.application import run as run_application
from sixnimmt.arena.bots.external_harnesses.mcp import run_stdio
from sixnimmt.arena.catalogue import CandidateConfig
from sixnimmt.arena.execution import RunExecutionError
from sixnimmt.arena.match import ArenaError
from sixnimmt.arena.planning import RunSettings, build_arena_plan
from sixnimmt.arena.results import MatchOutcome
from sixnimmt.arena.sessions import SetupTimeout, parse_seat
from sixnimmt.engine.replay import ReplayedMatch, replay_events
from sixnimmt.persistence.manifest import read_manifest_entry
from sixnimmt.persistence.sink import read_action_log, read_event_log
from sixnimmt.terminal import analysis_animation, arena_animation, table_display

_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_CANDIDATES: TypeAdapter[list[CandidateConfig]] = TypeAdapter(list[CandidateConfig])

app = typer.Typer(help="Run deterministic 6 nimmt! tools.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Run deterministic 6 nimmt! tools."""


@app.command()
def play(
    ctx: typer.Context,
    config_file: Annotated[
        Path | None, typer.Option("--config", help="JSON catalogue, schedule, and run settings.")
    ] = None,
    seat: Annotated[
        list[str] | None,
        typer.Option(
            "--seat", help="Fixed seats in order: catalogue key, bot, or bot:OPTIONS.json. Repeat for each seat."
        ),
    ] = None,
    games: Annotated[
        int | None,
        typer.Option(
            "--games", min=0, help="Games per lineup or player count; default 1 for fixed seats, otherwise 100."
        ),
    ] = None,
    player_count: Annotated[
        list[int] | None,
        typer.Option(
            "--player-count", min=2, max=10, help="Players per sampled game; default 4. Repeat for several sizes."
        ),
    ] = None,
    controlled_games: Annotated[
        int | None,
        typer.Option("--controlled-games", min=0, help="Additional games per selected opponent composition."),
    ] = None,
    seed: Annotated[int, typer.Option("--seed", help="Seed for reproducible games.")] = 66,
    hands: Annotated[int | None, typer.Option("--hands", min=1, help="End each match after this many hands.")] = None,
    communication: Annotated[
        bool,
        typer.Option("--communication/--no-communication", help="Allow messages and card changes before commitment."),
    ] = False,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", help="Save evidence and reports in this new directory; omit to run in memory."),
    ] = None,
    trace: Annotated[
        bool, typer.Option("--trace/--no-trace", help="Save detailed logs under traces/; requires --output-dir.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print the structured report as JSON.")] = False,
    watch: Annotated[
        bool, typer.Option("--watch/--no-watch", help="Watch each match; requires thread execution and concurrency 1.")
    ] = False,
    animation: Annotated[
        bool, typer.Option("--animation/--no-animation", help="Animate gameplay or progress in interactive terminals.")
    ] = True,
    commentary: Annotated[
        bool, typer.Option("--commentary/--no-commentary", help="Show private operator commentary while watching.")
    ] = True,
    quiet: Annotated[
        bool, typer.Option("--quiet", help="Omit board and activity output; keep launch instructions and results.")
    ] = False,
    concurrency: Annotated[int, typer.Option("--concurrency", min=1, help="Maximum matches running at once.")] = 1,
    backend: Annotated[str, typer.Option("--backend", help="Execution backend: thread or process.")] = "thread",
    scheduler: Annotated[str | None, typer.Option("--scheduler")] = None,
    decision_timeout: Annotated[
        float | None,
        typer.Option("--decision-timeout", help="Seconds allowed for each bot decision; setup is excluded."),
    ] = None,
    match_action_limit: Annotated[int, typer.Option("--match-action-limit", min=1)] = 10_000,
    play_action_limit: Annotated[int | None, typer.Option("--play-action-limit", min=1)] = None,
    decision_rejection_limit: Annotated[int, typer.Option("--decision-rejection-limit", min=1)] = 8,
    max_abandoned_decisions: Annotated[int | None, typer.Option("--max-abandoned-decisions", min=0)] = None,
    stop_on_failure: Annotated[bool, typer.Option("--stop-on-failure")] = False,
    auto_start: Annotated[
        bool, typer.Option("--auto-start", help="Start watched or attached sessions without confirmation.")
    ] = False,
    setup_timeout: Annotated[
        float, typer.Option("--setup-timeout", help="Seconds allowed for external seats to become ready.")
    ] = 600,
    managed_timeout: Annotated[
        float, typer.Option("--managed-timeout", help="Per-invocation timeout for managed clients.")
    ] = 120,
    memory: Annotated[
        bool, typer.Option("--memory/--no-memory", help="Enable an accepted private notebook for external seats.")
    ] = False,
    memory_max_chars: Annotated[int, typer.Option("--memory-max-chars", min=1, max=16_000)] = 4000,
    wait_timeout: Annotated[float, typer.Option("--wait-timeout", help="Maximum pending MCP play call.")] = 600,
    retain_seconds: Annotated[
        float, typer.Option("--retain-seconds", help="Retain completed attached-session replies for reconnects.")
    ] = 30,
) -> None:
    """Run fixed seats or sampled opponent schedules with shared execution and recording."""
    try:
        settings = _run_settings(ctx, config_file, seat)
        result = _execute_run(settings, json_output=json_output)
        _print_result(result, json_output=json_output)
        if result.run.status.error == "operator_stop" or any(
            record.reason == "operator_stop" for record in result.run.results
        ):
            raise typer.Exit(code=130)
        unsuccessful = any(
            record.outcome in (MatchOutcome.FAILED, MatchOutcome.ABANDONED) for record in result.run.results
        )
        if result.run.status.state != "completed":
            typer.echo(f"Execution {result.run.status.state}; the report includes incomplete coverage.", err=True)
        if unsuccessful or result.run.status.state != "completed":
            raise typer.Exit(code=1)
    except ValueError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    except RunExecutionError as error:
        if error.run.status.error == "operator_stop":
            typer.echo("Run stopped.", err=True)
            raise typer.Exit(code=130) from error
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    except (ArenaError, OSError, SetupTimeout) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    except KeyboardInterrupt as error:
        typer.echo("Run stopped.", err=True)
        raise typer.Exit(code=130) from error


def _provided(ctx: typer.Context, option: str) -> bool:
    source = ctx.get_parameter_source(option)
    return source is not None and source.name == "COMMANDLINE"


def _overrides(ctx: typer.Context, fields: dict[str, str]) -> dict[str, JsonValue]:
    """Translate explicitly supplied CLI values at the command boundary."""
    result: dict[str, JsonValue] = {}
    for option, field in fields.items():
        if _provided(ctx, option):
            value: object = ctx.params[option]
            if isinstance(value, Path):
                value = str(value)
            elif isinstance(value, tuple):
                value = list(value)
            result[field] = _JSON_VALUE.validate_python(value)
    return result


def _merge_section(values: dict[str, JsonValue], section: str, updates: dict[str, JsonValue]) -> None:
    if len(updates) == 0:
        return
    saved = values.get(section, {})
    if not isinstance(saved, dict):
        msg = f"{section} must be an object"
        raise ValueError(msg)  # noqa: TRY004 -- Invalid configuration shape.
    values[section] = {**saved, **updates}


def _run_settings(ctx: typer.Context, config_file: Path | None, seats: list[str] | None) -> RunSettings:
    values = {} if config_file is None else _JSON_OBJECT.validate_json(config_file.read_text(encoding="utf-8"))
    values.update(
        _overrides(
            ctx,
            {"games": "games", "player_count": "player_counts", "controlled_games": "controlled_games", "seed": "seed"},
        )
    )
    protocol = _overrides(ctx, {"communication": "communication_enabled", "hands": "hands"})
    if "hands" in protocol:
        protocol["end_condition"] = "fixed_hands"
    _merge_section(values, "protocol", protocol)
    _merge_section(
        values,
        "execution",
        _overrides(
            ctx,
            {
                "scheduler": "scheduler",
                "concurrency": "concurrency",
                "backend": "backend",
                "decision_timeout": "decision_timeout_seconds",
                "play_action_limit": "play_action_limit",
                "match_action_limit": "match_action_limit",
                "decision_rejection_limit": "decision_rejection_limit",
                "max_abandoned_decisions": "max_abandoned_decisions",
                "stop_on_failure": "stop_on_failure",
            },
        ),
    )
    _merge_section(
        values,
        "session",
        _overrides(
            ctx,
            {
                "auto_start": "auto_start",
                "setup_timeout": "setup_timeout",
                "managed_timeout": "managed_timeout_seconds",
                "memory": "memory_enabled",
                "memory_max_chars": "memory_max_chars",
                "wait_timeout": "wait_timeout_seconds",
                "retain_seconds": "retain_seconds",
            },
        ),
    )
    _merge_section(
        values,
        "display",
        _overrides(ctx, {"watch": "watch", "animation": "animation", "commentary": "commentary", "quiet": "quiet"}),
    )
    _merge_section(values, "recording", _overrides(ctx, {"output_dir": "output_dir", "trace": "trace"}))
    if seats is not None:
        _replace_lineup(values, seats)
    return RunSettings.model_validate(values)


def _replace_lineup(values: dict[str, JsonValue], specifications: list[str]) -> None:
    candidates = _CANDIDATES.validate_python(values.get("catalogue", []))
    keys = {candidate.bot if candidate.key is None else candidate.key for candidate in candidates}
    lineup: list[JsonValue] = []
    for specification in specifications:
        if specification in keys:
            lineup.append(specification)
            continue
        player = parse_seat(specification)
        existing = next(
            (
                candidate
                for candidate in candidates
                if candidate.bot == player.bot and candidate.options == player.options
            ),
            None,
        )
        if existing is not None:
            lineup.append(existing.bot if existing.key is None else existing.key)
            continue
        key = player.bot
        suffix = 2
        while key in keys:
            key = f"{player.bot}-{suffix}"
            suffix += 1
        candidates.append(CandidateConfig(bot=player.bot, key=key, options=player.options))
        keys.add(key)
        lineup.append(key)
    values["catalogue"] = [candidate.model_dump(mode="json") for candidate in candidates]
    values["lineup"] = lineup


def _confirm_start() -> bool:
    try:
        return typer.confirm("All seats ready. Start the match?", default=True, err=True)
    except typer.Abort as error:
        # Click translates both Ctrl-C and a closed input stream into Abort.
        # The run collector owns stopping workers and recording cancellation.
        raise KeyboardInterrupt from error


def _echo(text: str, *, stderr: bool) -> None:
    typer.echo(text, err=stderr)


def _execute_run(settings: RunSettings, *, json_output: bool) -> RunResult:
    plan = build_arena_plan(settings)
    message = f"Playing {len(plan.jobs)} games."
    if settings.recording.output_dir is not None:
        message += f" Saving results to {settings.recording.output_dir.resolve()}"
    typer.echo(message, err=True)
    animated = settings.display.animation and not settings.display.quiet and not json_output
    report: Callable[[str], None] = partial(_echo, stderr=json_output)
    try:
        with ExitStack() as presentation:
            observer = None
            on_activity = None
            on_progress = None
            if settings.display.watch:
                display = presentation.enter_context(
                    table_display(
                        enabled=animated,
                        quiet=settings.display.quiet or json_output,
                        commentary=settings.display.commentary,
                        report=report,
                    )
                )
                observer = display.observe
                on_activity = display.activity
                report = display.message
            else:
                catalogue = {entry.config_id: entry for entry in plan.catalogue}
                players = [catalogue[seat.config_id].player_config() for seat in plan.jobs[0].seats]
                progress = presentation.enter_context(
                    arena_animation(len(plan.jobs), plan.seed, players, enabled=animated)
                )
                on_progress = progress.update if progress is not None else None
            return run_application(
                settings,
                observer=observer,
                on_activity=on_activity,
                on_progress=on_progress,
                report=report,
                confirm_start=_confirm_start,
                on_analysis_started=partial(
                    _show_analysis, presentation, len(plan.jobs), len(plan.catalogue), plan.seed, animated
                ),
            )
    except (ValueError, ArenaError, OSError, SetupTimeout):
        raise
    except Exception as error:
        msg = f"run workflow failed: {error}"
        if settings.recording.output_dir is not None:
            msg += f". Completed data, if any: {settings.recording.output_dir.resolve()}"
        raise ArenaError(msg) from error


def _print_result(result: RunResult, *, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "report": result.report.model_dump(mode="json"),
                    "artifacts": None
                    if result.artifacts is None
                    else {
                        "directory": str(result.artifacts.report.parent),
                        "analysis": str(result.artifacts.analysis),
                        "report": str(result.artifacts.report),
                    },
                },
                indent=2,
            )
        )
    else:
        Console(highlight=False).print(terminal_report(result.report, artifacts=result.artifacts))


def _show_analysis(stack: ExitStack, matches: int, strategies: int, seed: int, enabled: bool) -> None:
    stack.close()
    stack.enter_context(analysis_animation(matches, strategies, seed, enabled=enabled))


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
    path: Annotated[Path, typer.Argument(help="A match event log.")],
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
            entry = read_manifest_entry(manifest_path, path.name)
        summary = summarise_match(events, actions, entry)
    except (OSError, ValueError, KeyError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    typer.echo(summary.model_dump_json(indent=2))


@app.command(name="harness-mcp")
def harness_mcp() -> None:
    """Serve the two seat-scoped game tools over stateless MCP stdio."""
    run_stdio()
