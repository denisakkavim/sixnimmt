"""Readable terminal tables for arena results and strategy comparisons."""

from collections import defaultdict
from io import StringIO
from pathlib import Path

from rich import box
from rich.console import Console, Group, RenderableType
from rich.table import Table
from rich.text import Text

from sixnimmt.analytics.models import Estimate, EvaluationReport


def _label(report: EvaluationReport, config_id: str) -> str:
    label = report.catalogue.get(config_id, config_id)
    return label.replace("_", " ") if label.islower() else label


def _metric(estimate: Estimate, *, difference: bool = False) -> Text:
    if estimate.value is None:
        return Text("—", style="dim")
    point = f"{estimate.value * 100:+.1f} pp" if difference else f"{estimate.value:.1%}"
    result = Text(point, style="bold cyan")
    interval = estimate.interval
    if interval is None:
        result.append("\n—", style="dim")
    elif difference:
        result.append(f"\n{interval.low * 100:+.1f}…{interval.high * 100:+.1f}", style="dim")
    else:
        result.append(f"\n{interval.low:.1%}-{interval.high:.1%}", style="dim")
    return result


def _average_score(report: EvaluationReport, estimate: Estimate) -> str:
    for profile in report.outcome_profiles:
        if (
            profile.configuration_id == estimate.configuration_id
            and profile.player_count == estimate.player_count
            and profile.population_id == estimate.population_id
            and profile.view == estimate.view
            and profile.mean_penalty is not None
        ):
            return f"{profile.mean_penalty:.1f}"
    return "—"


def _summary(report: EvaluationReport) -> list[RenderableType]:
    diagnostics = report.diagnostics
    items: list[RenderableType] = [
        Text("\n6 nimmt! · Arena results", style="bold cyan"),
        Text(
            f"{diagnostics.planned_matches:,} planned · {diagnostics.started_matches:,} started · "
            f"{len(report.catalogue):,} strategies · {report.run_status.replace('_', ' ')}",
            style="dim",
        ),
    ]
    outcomes = Table(box=box.SIMPLE, padding=(0, 1))
    counts = (
        ("Finished", diagnostics.finished_matches, "green"),
        ("Abandoned", diagnostics.outcomes.get("abandoned", 0), "yellow"),
        ("Forfeited", diagnostics.outcomes.get("forfeited", 0), "yellow"),
        ("Failed", diagnostics.outcomes.get("failed", 0), "red"),
        ("Missing", diagnostics.missing_matches, "yellow"),
    )
    for label, count, style in counts:
        outcomes.add_column(label, justify="right", header_style="bold", style=style if count > 0 else "dim")
    outcomes.add_row(*(f"{count:,}" for _, count, _ in counts))
    items.append(outcomes)
    protocol = report.context["protocol"]
    mode = "Communication enabled" if protocol["communication_enabled"] else "Classic rules"
    ending = (
        f"{protocol['hands']} completed hands"
        if protocol["end_condition"] == "fixed_hands"
        else f"first completed hand reaching {report.context['rules']['target_score']} points"
    )
    items.append(Text(f"{mode} · {ending}", style="dim"))
    items.append(
        Text(
            f"Independent samples: {diagnostics.completed_blocks:,}/{diagnostics.independent_blocks:,} complete",
            style="dim",
        )
    )
    return items


def _result_table(report: EvaluationReport, estimates: list[Estimate]) -> Table:
    cutoff = report.analysis_spec.cutoff(estimates[0].player_count)
    table = Table(box=box.SIMPLE_HEAD, header_style="bold", padding=(0, 1), leading=1)
    table.add_column("Strategy", overflow="fold", ratio=2)
    table.add_column("Games", justify="right")
    table.add_column("Win share", justify="right")
    table.add_column(f"Top {cutoff}", justify="right")
    table.add_column("Avg score", justify="right")
    rows: dict[tuple[str, int | None], dict[str, Estimate]] = defaultdict(dict)
    for estimate in estimates:
        rows[(estimate.configuration_id, estimate.seat)][estimate.objective] = estimate
    for (config_id, seat), values in rows.items():
        win = values["win_credit"]
        label = _label(report, config_id)
        if seat is not None:
            label += f" (seat {seat + 1})"
        coverage = win.coverage
        table.add_row(
            Text(label, style="bold"),
            f"{coverage.finished_appearances:,}/{coverage.planned_appearances:,}",
            _metric(win),
            _metric(values["acceptable_credit"]),
            _average_score(report, win),
        )
    return table


def _population_tables(report: EvaluationReport) -> list[RenderableType]:
    primary = [estimate for estimate in report.population_estimates if estimate.view in ("iid", "fixed")]
    if len(primary) == 0:
        primary = [estimate for estimate in report.population_estimates if estimate.view == "weighted"]
    groups: dict[tuple[int, str | None, str], list[Estimate]] = defaultdict(list)
    for estimate in primary:
        groups[(estimate.player_count, estimate.population_id, estimate.view)].append(estimate)
    items: list[RenderableType] = []
    for (count, population, stream), estimates in groups.items():
        sample = {"iid": "random lineups", "fixed": "fixed lineup", "weighted": "weighted opponents"}[stream]
        title = f"\n{count} players · {sample}"
        if population is not None:
            title += f" · {population.replace('_', ' ')}"
        items.extend([Text(title, style="bold"), _result_table(report, estimates)])
        absent = [
            _label(report, estimate.configuration_id)
            for estimate in estimates
            if estimate.objective == "win_credit" and estimate.coverage.planned_appearances == 0
        ]
        if len(absent) > 0:
            items.append(Text("No results: " + ", ".join(absent) + " (no planned appearances).", style="dim"))
    return items


def _comparison_tables(report: EvaluationReport) -> list[RenderableType]:
    groups: dict[tuple[str | None, int, str | None], list[Estimate]] = defaultdict(list)
    for estimate in report.comparisons:
        if estimate.condition_id is None:
            groups[(estimate.comparison_id, estimate.player_count, estimate.population_id)].append(estimate)
    items: list[RenderableType] = []
    for (name, count, population), estimates in groups.items():
        first = estimates[0]
        reference = _label(report, first.reference_id) if first.reference_id is not None else "reference"
        title = f"\n{_label(report, first.configuration_id)} vs {reference} · {count} players"
        items.append(Text(title, style="bold"))
        context = f"{name} · {population}" if population is not None else str(name)
        items.append(Text(context, style="dim"))
        table = Table(box=box.SIMPLE_HEAD, header_style="bold", padding=(0, 1), leading=1)
        table.add_column("Measure")
        table.add_column("Difference", justify="right")
        table.add_column("Assessment", overflow="fold")
        for estimate in estimates:
            objective = (
                "Win share" if estimate.objective == "win_credit" else f"Top {report.analysis_spec.cutoff(count)}"
            )
            assessment = estimate.interpretation.replace("_", " ") if estimate.interpretation is not None else "—"
            table.add_row(objective, _metric(estimate, difference=True), Text(assessment))
        items.append(table)
        items.append(
            Text(
                f"{first.coverage.complete_blocks:,} completed comparisons · "
                f"{first.coverage.incomplete_pairs:,} incomplete pairs. "
                f"Positive favors the candidate; practical threshold {report.analysis_spec.practical_effect_threshold * 100:.1f} pp.",
                style="dim",
            )
        )
    return items


def terminal_report(report: EvaluationReport) -> Group:
    """Build tables that adapt to the console width and keep user labels literal."""
    items = _summary(report)
    items.extend(_population_tables(report))
    items.extend(_comparison_tables(report))
    items.append(
        Text("Games = finished/planned strategy appearances. A strategy can occupy several seats.", style="dim")
    )
    items.append(
        Text("Win share splits tied wins. Top finishes also share ties. Lower average score is better.", style="dim")
    )
    items.append(
        Text(
            f"Ranges below percentages are {report.analysis_spec.confidence_level:.0%} confidence intervals. "
            "— means insufficient data or missing opponent coverage. Results use finished games.",
            style="dim",
        )
    )
    if any(estimate.status == "unsupported" for estimate in report.population_estimates):
        message = "Some weighted comparisons need more opponent coverage."
        if report.artifact_dir is not None:
            message += " See the saved report."
        items.append(Text(message, style="yellow"))
    if report.analysis_spec.evidence_label != "unspecified":
        items.append(Text(f"Evidence: {report.analysis_spec.evidence_label}.", style="dim"))
    if report.artifact_dir is not None:
        directory = Path(report.artifact_dir)
        if directory.is_relative_to(Path.cwd()):
            directory = directory.relative_to(Path.cwd())
        items.append(Text(f"\nFull report  {directory}/report.md", style="cyan"))
        items.append(Text(f"Saved data   {directory}/analysis.json.gz", style="dim"))
    return Group(*items)


def report_terminal(report: EvaluationReport, *, width: int = 100) -> str:
    """Return an aligned, color-free summary for callers saving terminal text."""
    output = StringIO()
    console = Console(file=output, width=width, color_system=None, highlight=False)
    console.print(terminal_report(report))
    return output.getvalue().rstrip()
