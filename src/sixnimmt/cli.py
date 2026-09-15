"""Command-line interface for comparisons, watched tables and match inspection."""

import gzip
import json
import math
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from rich.console import Console

from sixnimmt.analytics.evaluation import analyse_run
from sixnimmt.analytics.reporting import report_markdown
from sixnimmt.analytics.summary import summarise as summarise_match
from sixnimmt.analytics.terminal import terminal_report
from sixnimmt.arena.bots.external_harnesses.mcp import run_stdio
from sixnimmt.arena.config import RunConfig, resolve
from sixnimmt.arena.match import ArenaError
from sixnimmt.arena.planned import run_plan
from sixnimmt.arena.planning import LineupConfig, build_arena_plan
from sixnimmt.arena.results import MatchOutcome
from sixnimmt.arena.table import SetupTimeout, TableConfig, run_table
from sixnimmt.engine.replay import ReplayedMatch, replay_events
from sixnimmt.engine.rules import EndCondition
from sixnimmt.persistence.manifest import ManifestMatch
from sixnimmt.persistence.sink import read_action_log, read_event_log
from sixnimmt.terminal import analysis_animation, arena_animation, table_display

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


@dataclass(frozen=True)
class ComparisonOptions:
    config_file: Path | None = None
    player_counts: tuple[int, ...] | None = None
    games: int | None = None
    controlled_games: int | None = None
    output_dir: Path | None = None
    trace: bool = False
    json_output: bool = False
    animation: bool = True


def _provided(ctx: typer.Context, option: str) -> bool:
    source = ctx.get_parameter_source(option)
    return source is not None and source.name == "COMMANDLINE"


def _execution_settings(ctx: typer.Context, saved: RunConfig, supplied: RunConfig) -> RunConfig:
    names = {
        "scheduler": "scheduler",
        "concurrency": "concurrency",
        "backend": "backend",
        "decision_timeout": "decision_timeout_seconds",
        "play_action_limit": "play_action_limit",
        "decision_rejection_limit": "decision_rejection_limit",
        "max_abandoned_decisions": "max_abandoned_decisions",
        "stop_on_failure": "stop_on_failure",
        "match_action_limit": "match_action_limit",
    }
    overrides: dict[str, Any] = {}
    for option, field in names.items():
        if _provided(ctx, option):
            overrides[field] = getattr(supplied, field)
    return replace(saved, **overrides)


def _lineup_settings(
    ctx: typer.Context, options: ComparisonOptions, config: RunConfig, seed: int, communication: bool
) -> LineupConfig:
    settings = (
        LineupConfig()
        if options.config_file is None
        else LineupConfig.model_validate_json(options.config_file.read_text(encoding="utf-8"))
    )
    values = settings.model_dump()
    for field in ("player_counts", "games", "controlled_games"):
        value = getattr(options, field)
        if value is not None:
            values[field] = value
    if _provided(ctx, "seed"):
        values["seed"] = seed
    if _provided(ctx, "communication"):
        values["protocol"] = settings.protocol.model_copy(update={"communication_enabled": communication})
    values["execution"] = _execution_settings(ctx, settings.execution, config)
    return LineupConfig.model_validate(values)


def run_comparison(
    ctx: typer.Context,
    options: ComparisonOptions,
    config: RunConfig,
    *,
    seed: int,
    communication: bool,
) -> None:
    if options.trace and options.output_dir is None:
        msg = "--trace requires --output-dir"
        raise ValueError(msg)
    settings = _lineup_settings(ctx, options, config, seed, communication)
    plan = build_arena_plan(settings)
    output_dir = options.output_dir
    message = f"Playing {len(plan.jobs)} games."
    if output_dir is not None:
        message += f" Saving results to {output_dir.resolve()}"
    try:
        typer.echo(message, err=True)
        catalogue = {entry.config_id: entry for entry in plan.catalogue}
        players = [catalogue[seat.config_id].player_config() for seat in plan.jobs[0].seats]
        with arena_animation(
            len(plan.jobs), plan.seed, players, enabled=options.animation and not options.json_output
        ) as display:
            run = run_plan(
                plan,
                output_dir=output_dir,
                trace=options.trace,
                on_progress=display.update if display is not None else None,
            )
        with analysis_animation(
            len(run.results), len(plan.catalogue), plan.seed, enabled=options.animation and not options.json_output
        ):
            report = analyse_run(run)
            if output_dir is not None:
                (output_dir / "analysis.json.gz").write_bytes(
                    gzip.compress(report.model_dump_json().encode("utf-8"), mtime=0)
                )
                (output_dir / "report.md").write_text(report_markdown(report), encoding="utf-8")
    except Exception as error:
        msg = f"comparison workflow failed: {error}"
        if output_dir is not None:
            msg += f". Completed data, if any: {output_dir.resolve()}"
        raise ArenaError(msg) from error
    if options.json_output:
        typer.echo(
            json.dumps(
                {
                    "report": report.model_dump(mode="json"),
                    "artifacts": None
                    if output_dir is None
                    else {
                        "directory": str(output_dir.resolve()),
                        "analysis": str((output_dir / "analysis.json.gz").resolve()),
                        "report": str((output_dir / "report.md").resolve()),
                    },
                },
                indent=2,
            )
        )
    else:
        Console(highlight=False).print(terminal_report(report))
    if run.status.state != "completed":
        typer.echo(f"Execution {run.status.state}; the report includes incomplete coverage.", err=True)
        raise typer.Exit(code=1)


def _validate_duration(name: str, value: float, *, allow_zero: bool = False) -> None:
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        msg = f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}"
        raise ValueError(msg)


def _confirm_table_start() -> bool:
    return typer.confirm("All seats ready. Start the match?", default=True)


def _table_settings(
    ctx: typer.Context,
    config_file: Path | None,
    seed: int,
    hands: int | None,
    communication: bool,
    execution: RunConfig,
) -> TableConfig:
    settings = (
        TableConfig()
        if config_file is None
        else TableConfig.model_validate_json(config_file.read_text(encoding="utf-8"))
    )
    values = settings.model_dump()
    if _provided(ctx, "seed"):
        values["seed"] = seed
    protocol = settings.protocol.model_dump()
    if _provided(ctx, "hands"):
        protocol["hands"] = hands
        protocol["end_condition"] = EndCondition.FIXED_HANDS
    if _provided(ctx, "communication"):
        protocol["communication_enabled"] = communication
    values["protocol"] = protocol
    values["execution"] = _execution_settings(ctx, settings.execution, execution)
    return TableConfig.model_validate(values)


@app.command()
def table(
    ctx: typer.Context,
    seat: Annotated[
        list[str] | None,
        typer.Option(
            "--seat",
            help="Replace the lineup with these catalogue keys, bot names, or bot:OPTIONS.json seats, in order.",
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="JSON catalogue, fixed lineup, game rules, and execution settings."),
    ] = None,
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", help="New directory for private seat bundles and replay traces.")
    ] = None,
    seed: Annotated[int, typer.Option("--seed")] = 66,
    hands: Annotated[
        int | None,
        typer.Option("--hands", min=1, help="End after this many hands instead of reaching the target score."),
    ] = None,
    communication: Annotated[bool, typer.Option("--communication")] = False,
    auto_start: Annotated[
        bool, typer.Option("--auto-start", help="Start as soon as all external seats call play().")
    ] = False,
    setup_timeout: Annotated[
        float, typer.Option("--setup-timeout", help="Seconds allowed for agents to enter the waiting room.")
    ] = 600,
    decision_timeout: Annotated[
        float | None, typer.Option("--decision-timeout", help="Optional per-decision limit; setup is excluded.")
    ] = None,
    managed_timeout: Annotated[
        float, typer.Option("--managed-timeout", help="Finite per-invocation timeout for managed clients.")
    ] = 120,
    memory: Annotated[
        bool, typer.Option("--memory", help="Enable an arena-accepted notebook for external seats.")
    ] = False,
    memory_max_chars: Annotated[int, typer.Option("--memory-max-chars", min=1, max=16_000)] = 4000,
    wait_timeout: Annotated[
        float, typer.Option("--wait-timeout", help="Maximum pending MCP play call, below the generated client timeout.")
    ] = 600,
    retain_seconds: Annotated[
        float, typer.Option("--retain-seconds", help="Keep completed seat results available for reconnects.")
    ] = 30,
    match_action_limit: Annotated[int, typer.Option("--match-action-limit", min=1)] = 10_000,
    quiet: Annotated[bool, typer.Option("--quiet", help="Omit board updates; keep setup and result messages.")] = False,
    animation: Annotated[
        bool, typer.Option("--animation/--no-animation", help="Animate actual gameplay on interactive terminals.")
    ] = True,
    commentary: Annotated[
        bool,
        typer.Option(
            "--commentary/--no-commentary", help="Show private operator commentary from agents and simulations."
        ),
    ] = True,
) -> None:
    """Watch one game with fixed seats in their native terminals or supervised processes."""
    try:
        _validate_duration("setup_timeout", setup_timeout)
        _validate_duration("managed_timeout", managed_timeout)
        _validate_duration("wait_timeout", wait_timeout)
        _validate_duration("retain_seconds", retain_seconds, allow_zero=True)
        directory = output_dir if output_dir is not None else Path.cwd() / f"sixnimmt-table-{uuid.uuid4().hex[:8]}"
        directory = directory.resolve()
        settings = _table_settings(
            ctx,
            config_file,
            seed,
            hands,
            communication,
            RunConfig(decision_timeout_seconds=decision_timeout, match_action_limit=match_action_limit),
        )
        players = settings.players(seat)
        config = resolve(replace(settings.execution, trace_dir=directory / "traces"), settings.protocol)
        with table_display(enabled=animation, quiet=quiet, commentary=commentary, report=typer.echo) as display:
            result = run_table(
                players,
                directory,
                settings.seed,
                settings.rules,
                settings.protocol,
                config,
                setup_timeout=setup_timeout,
                auto_start=auto_start,
                retain_seconds=retain_seconds,
                wait_timeout_seconds=wait_timeout,
                managed_timeout_seconds=managed_timeout,
                memory_enabled=memory,
                memory_max_chars=memory_max_chars,
                report=typer.echo,
                confirm_start=_confirm_table_start,
                observer=display.observe,
                on_activity=display.activity,
            )
        if result.reason == "operator_stop":
            raise typer.Exit(code=130)
        if result.outcome == MatchOutcome.FAILED:
            raise typer.Exit(code=1)
    except (ValueError, SetupTimeout) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    except (ArenaError, OSError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    except KeyboardInterrupt as error:
        typer.echo("Table stopped before match start.")
        raise typer.Exit(code=130) from error


@app.command(name="harness-mcp")
def harness_mcp() -> None:
    """Serve the two seat-scoped game tools over stateless MCP stdio."""
    run_stdio()
