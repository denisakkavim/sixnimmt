"""Command orchestration for strategy comparisons and optional saved results."""

import gzip
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from sixnimmt.analytics.evaluation import analyse_run
from sixnimmt.analytics.reporting import report_markdown
from sixnimmt.analytics.terminal import terminal_report
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.match import ArenaError
from sixnimmt.arena.planned import run_plan
from sixnimmt.arena.planning import LineupConfig, build_arena_plan
from sixnimmt.terminal import analysis_animation, arena_animation


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
