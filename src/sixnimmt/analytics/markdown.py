"""Readable saved reports with concise results and expandable supporting evidence."""

import json
import re
from collections import Counter, defaultdict
from collections.abc import Sequence

from sixnimmt.analytics.models import Estimate, EvaluationReport, OutcomeProfile
from sixnimmt.analytics.projection import (
    ResultRow,
    average_score,
    comparison_groups,
    configuration_label,
    population_groups,
    result_rows,
)


def _escape(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "\\|")
        .replace("\n", " ")
    )


def _human(value: str) -> str:
    words = value.replace("_", " ")
    return words[:1].upper() + words[1:]


def _composition(report: EvaluationReport, opponents: tuple[str, ...]) -> str:
    counts = Counter(opponents)
    parts = []
    for config_id in sorted(counts, key=lambda item: configuration_label(report, item)):
        count = counts[config_id]
        label = configuration_label(report, config_id)
        parts.append(f"{label} x{count}" if count > 1 else label)
    return ", ".join(parts)


def _conditions(report: EvaluationReport) -> dict[str, str]:
    identifiers: set[str] = set()
    for estimate in (*report.seat_order_estimates, *report.comparisons, *report.condition_effects):
        if estimate.condition_id is not None:
            identifiers.update(re.findall(r"(?:condition|composition)-[a-f0-9]+", estimate.condition_id))
    return {identity: f"Lineup {index + 1}" for index, identity in enumerate(sorted(identifiers))}


def _condition(value: str, aliases: dict[str, str]) -> str:
    for identity, label in aliases.items():
        value = value.replace(identity, label)
    return _human(value)


def _percent(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "—"


def _result(estimate: Estimate | None) -> str:
    if estimate is None or estimate.value is None:
        return "—"
    difference = estimate.reference_id is not None
    point = f"{estimate.value * 100:+.1f} pp" if difference else _percent(estimate.value)
    if estimate.interval is None:
        return f"**{point}** †"
    low, high = estimate.interval.low * 100, estimate.interval.high * 100
    unit = " pp" if difference else "%"
    return f"**{point}**<br>{low:.1f}&ndash;{high:.1f}{unit}"


def _bounds(estimate: Estimate | None) -> str:
    if estimate is None or estimate.missing_outcome_bounds is None:
        return "—"
    low, high = estimate.missing_outcome_bounds
    unit = " pp" if estimate.reference_id is not None else "%"
    return f"{low * 100:.1f}&ndash;{high * 100:.1f}{unit}"


def _row_context(item: Estimate, report: EvaluationReport, aliases: dict[str, str]) -> str:
    parts: list[str] = []
    if item.opponents is not None:
        parts.append(_composition(report, item.opponents))
    elif item.condition_id is not None:
        parts.append(_condition(item.condition_id, aliases))
    if item.copy_count is not None:
        parts.append(f"{item.copy_count} copies at the table")
    if item.seat is not None:
        parts.append(f"Seat {item.seat + 1}")
    if item.permutation is not None:
        parts.append(f"Order {int(item.permutation) + 1}" if item.permutation.isdigit() else item.permutation)
    return " · ".join(parts)


def _result_table(
    estimates: Sequence[Estimate], report: EvaluationReport, aliases: dict[str, str], limit: int | None = None
) -> list[str]:
    if len(estimates) == 0:
        return []
    cutoff = report.analysis_spec.cutoff(estimates[0].player_count)
    rows = result_rows(estimates)
    total = len(rows)
    if limit is not None and total > limit:
        rows = sorted(rows, key=_coverage_order, reverse=True)[:limit]
    contexts = [_row_context(row.context, report, aliases) for row in rows]
    with_context = any(value != "" for value in contexts)
    with_interpretation = any(row.context.interpretation is not None for row in rows)
    row_scores = [average_score(report, row.context) for row in rows]
    with_score = any(score is not None for score in row_scores)
    headings = ["Strategy"] + (["Opponents / setting"] if with_context else []) + ["Win", f"Top {cutoff}"]
    headings.extend(["Avg score"] if with_score else [])
    headings.extend(["Appearances", "Samples"])
    headings.extend(["Interpretation"] if with_interpretation else [])
    lines: list[str] = ["| " + " | ".join(headings) + " |", "| " + " | ".join("---" for _ in headings) + " |"]
    for row, context, score in zip(rows, contexts, row_scores, strict=True):
        lines.append(_table_row(row, context, score, report, with_context, with_score, with_interpretation))
    if total > len(rows):
        lines.extend([
            "",
            f"Showing {len(rows):,} of {total:,} settings, ordered by completed samples. [Full results](analysis.json.gz).",
        ])
    return lines


def _coverage_order(row: ResultRow) -> tuple[int, int]:
    return row.context.coverage.complete_blocks, row.context.coverage.finished_appearances


def _table_row(
    row: ResultRow,
    context: str,
    score: float | None,
    report: EvaluationReport,
    with_context: bool,
    with_score: bool,
    with_interpretation: bool,
) -> str:
    item = row.context
    label = configuration_label(report, item.configuration_id)
    if item.reference_id is not None:
        label += " vs " + configuration_label(report, item.reference_id)
    values = [_escape(label)]
    if with_context:
        values.append(_escape(context))
    values.extend([_result(row.win), _result(row.acceptable)])
    if with_score:
        values.append(_number(score))
    coverage = item.coverage
    values.extend([
        f"{coverage.finished_appearances:,} / {coverage.planned_appearances:,}",
        f"{coverage.complete_blocks:,} / {coverage.planned_blocks:,}",
    ])
    if with_interpretation:
        cutoff = report.analysis_spec.cutoff(item.player_count)
        values.append(
            "<br>".join(
                f"{label}: {estimate.interpretation}"
                for label, estimate in (("Win", row.win), (f"Top {cutoff}", row.acceptable))
                if estimate is not None and estimate.interpretation is not None
            )
        )
    return "| " + " | ".join(values) + " |"


def _details(title: str, content: list[str]) -> list[str]:
    return ["<details>", f"<summary>{_escape(title)}</summary>", "", *content, "", "</details>", ""]


def _group_title(key: tuple[int, str | None, str]) -> str:
    count, population, view = key
    names = {"iid": "random lineups", "fixed": "fixed lineup", "weighted": "weighted opponents"}
    parts = [f"{count} players"]
    if population is not None:
        parts.append(_human(population))
    parts.append(names.get(view, _human(view)))
    return " · ".join(parts)


def _population_sections(report: EvaluationReport, aliases: dict[str, str]) -> list[str]:
    groups = population_groups(report.population_estimates)
    lines = ["## Results", ""]
    unavailable: list[str] = []
    for key, estimates in groups.items():
        title = _group_title(key)
        if all(item.value is None for item in estimates):
            unavailable.append(title)
            continue
        lines.extend([f"### {_escape(title)}", "", *_result_table(estimates, report, aliases), ""])
    if len(unavailable) > 0:
        lines.extend([
            "**Not yet estimable:** "
            + "; ".join(_escape(title) for title in unavailable)
            + ". Finished observations or required opponent combinations are missing; see opponent coverage below.",
            "",
        ])
    return lines


def _comparison_sections(report: EvaluationReport, aliases: dict[str, str]) -> list[str]:
    if len(report.comparisons) == 0:
        return []
    groups = comparison_groups(report.comparisons)
    lines = [
        "## Replacement comparisons",
        "",
        f"Effects are percentage-point changes from the reference. The declared useful-change threshold is **{report.analysis_spec.practical_effect_threshold * 100:.1f} pp**.",
        "",
    ]
    for key, estimates in groups.items():
        comparison, count, population = key.comparison_id, key.player_count, key.population_id
        title = f"{_human(comparison) if comparison is not None else 'Comparison'} · {count} players"
        if population is not None:
            title += " · " + _human(population)
        lines.extend([
            f"### {_escape(title)}",
            "",
            *_result_table([item for item in estimates if item.condition_id is None], report, aliases),
            "",
        ])
        detailed = [item for item in estimates if item.condition_id is not None]
        if len(detailed) > 0:
            lines.extend(
                _details("Results against each opponent lineup", _result_table(detailed, report, aliases, limit=40))
            )
    return lines


def _coverage_sections(report: EvaluationReport, aliases: dict[str, str]) -> list[str]:
    estimates = [
        item
        for item in (*report.population_estimates, *report.comparisons)
        if item.objective == "win_credit" and (item.target_cell_count is not None or len(item.cells) > 0)
    ]
    if len(estimates) == 0:
        return []
    groups: dict[tuple[int, str | None, str | None], list[Estimate]] = defaultdict(list)
    for item in estimates:
        groups[(item.player_count, item.population_id, item.comparison_id)].append(item)
    lines = [
        "## Opponent coverage",
        "",
        "Weighted results require finished games against every opponent combination in the declared population. Missing combinations retain their original weights; they are never treated as losses or removed from the average.",
        "",
    ]
    for (count, population, comparison), items in groups.items():
        title = f"{count} players · {_human(population) if population is not None else 'Declared opponents'}"
        if comparison is not None:
            title += " · " + _human(comparison)
        table = ["| Strategy | Opponent combinations covered | Missing population weight |", "| --- | --- | --- |"]
        for item in items:
            total = item.target_cell_count if item.target_cell_count is not None else len(item.cells)
            covered = sum(cell.finished_blocks > 0 for cell in item.cells)
            missing_weight = item.unlisted_cell_weight + sum(
                cell.weight for cell in item.cells if cell.finished_blocks == 0
            )
            table.append(
                f"| {_escape(configuration_label(report, item.configuration_id))} | {covered:,} / {total:,} | {missing_weight:.1%} |"
            )
        matching = [
            item
            for item in (*report.population_estimates, *report.comparisons)
            if item.player_count == count
            and item.population_id == population
            and item.comparison_id == comparison
            and (item.target_cell_count is not None or len(item.cells) > 0)
        ]
        table.extend([
            "",
            "**Possible results if missing games finished**",
            "",
            *_bound_table(matching, report, aliases),
        ])
        lines.extend(_details(title, table))
    lines.extend([
        "Full combination weights and support counts are retained in [structured analysis](analysis.json.gz). Large unplayed populations are represented there by their exact count and remaining probability; unplayed combinations are not listed individually in this report.",
        "",
    ])
    return lines


def _bound_table(estimates: Sequence[Estimate], report: EvaluationReport, aliases: dict[str, str]) -> list[str]:
    rows = [
        row
        for row in result_rows(estimates)
        if any(
            item is not None
            and item.missing_outcome_bounds is not None
            and (item.value is None or item.missing_outcome_bounds[0] != item.missing_outcome_bounds[1])
            for item in (row.win, row.acceptable)
        )
    ]
    if len(rows) == 0:
        return ["No missing-outcome uncertainty in this view."]
    lines = ["| Strategy / setting | Win bounds | Qualifying-finish bounds |", "| --- | --- | --- |"]
    for row in rows:
        label = configuration_label(report, row.context.configuration_id)
        context = _row_context(row.context, report, aliases)
        if context != "":
            label += ": " + context
        lines.append(f"| {_escape(label)} | {_bounds(row.win)} | {_bounds(row.acceptable)} |")
    return lines


def _supporting_results(report: EvaluationReport, aliases: dict[str, str]) -> list[str]:
    sections = (
        ("Opponent combinations", report.composition_estimates),
        ("Copies of the same strategy", report.copy_count_estimates),
        ("Seat orders", report.seat_order_estimates),
        ("Changes between conditions", report.condition_effects),
        ("Weakest tested opponents", report.weakest_cells),
    )
    lines = [
        "## Detailed results",
        "",
        "These views describe the conditions tested. Selected weak spots and subgroup comparisons are exploratory; a noisy minimum is not evidence of a universal weakness.",
        "",
    ]
    for title, estimates in sections:
        if len(estimates) == 0:
            continue
        groups: dict[int, list[Estimate]] = defaultdict(list)
        for item in estimates:
            groups[item.player_count].append(item)
        content: list[str] = []
        for count, group in groups.items():
            content.extend([f"### {count} players", "", *_result_table(group, report, aliases, limit=40), ""])
        lines.extend(_details(title, content))
    missing = [
        item
        for item in (
            *report.population_estimates,
            *report.comparisons,
            *report.composition_estimates,
            *report.copy_count_estimates,
            *report.seat_order_estimates,
            *report.condition_effects,
        )
        if item.view != "weighted" and item.coverage.finished_appearances < item.coverage.planned_appearances
    ]
    if len(missing) > 0:
        lines.extend(_details("Bounds for unfinished games", _bound_table(missing, report, aliases)))
    return lines


def _number(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.{digits}f}"


def _profile_context(profile: OutcomeProfile, report: EvaluationReport) -> str:
    if profile.opponents is not None:
        return _composition(report, profile.opponents)
    return _human(profile.population_id) if profile.population_id is not None else "Overall"


def _profiles(report: EvaluationReport) -> list[str]:
    if len(report.outcome_profiles) == 0:
        return []
    groups: dict[int, list[OutcomeProfile]] = defaultdict(list)
    for profile in report.outcome_profiles:
        groups[profile.player_count].append(profile)
    lines = [
        "Finishing distributions run from first place to last. Ties share their occupied places equally. Penalties include finished games and completed hands only.",
        "",
    ]
    for count, profiles in groups.items():
        total = len(profiles)
        profiles = sorted(
            profiles, key=lambda profile: (profile.view == "iid", profile.finished_appearances), reverse=True
        )[:40]
        lines.extend([
            f"### {count} players",
            "",
            "| Strategy / opponents | Place distribution, % | Sole wins | Shared first | Mean penalty | Per hand | Hand appearances |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ])
        for profile in profiles:
            distribution = (
                " / ".join(f"{value * 100:.1f}" for value in profile.finishing_distribution)
                if profile.finishing_distribution is not None
                else "—"
            )
            label = configuration_label(report, profile.configuration_id) + ": " + _profile_context(profile, report)
            values = [
                _escape(label),
                distribution,
                _percent(profile.sole_win_frequency),
                _percent(profile.shared_first_frequency),
                _number(profile.mean_penalty),
                _number(profile.finished_hand_penalty, 2),
                _number(profile.completed_hand_appearances),
            ]
            lines.append("| " + " | ".join(values) + " |")
        lines.append("")
        if total > len(profiles):
            lines.extend([
                f"Showing {len(profiles):,} of {total:,} finishing profiles. [Full results](analysis.json.gz).",
                "",
            ])
    return _details("Finishing positions and penalties", lines)


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 1:
        return f"{seconds * 1000:,.2f} ms"
    return f"{seconds:,.3f} s"


def _diagnostics(report: EvaluationReport) -> list[str]:
    measured = report.diagnostics
    values = [
        (
            "Games planned / started / returned",
            f"{measured.planned_matches:,} / {measured.started_matches:,} / {measured.returned_matches:,}",
        ),
        (
            "Finished / unsuccessful / missing",
            f"{measured.finished_matches:,} / {measured.unsuccessful_matches:,} / {measured.missing_matches:,}",
        ),
        ("Independent samples completed / planned", f"{measured.completed_blocks:,} / {measured.independent_blocks:,}"),
        ("Actions accepted / rejected", f"{measured.actions_accepted:,} / {measured.actions_rejected:,}"),
        ("Timed bot calls", _number(measured.measured_decision_calls)),
        ("Total bot-call time", _duration(measured.decision_seconds_total)),
        (
            "Median / 95th-percentile bot-call time",
            _number(measured.decision_seconds_median, 3) + " / " + _number(measured.decision_seconds_p95, 3) + " s",
        ),
        ("Total measured game time", _duration(measured.match_seconds_total)),
    ]
    lines = ["## Completion and resources", "", "| Measurement | Result |", "| --- | --- |"]
    lines.extend(f"| {label} | {value} |" for label, value in values)
    lines.append("")
    if len(measured.failure_reasons) == 0 and measured.unsuccessful_matches == 0:
        lines.extend(["No unsuccessful games were recorded.", ""])
    else:
        failures = ["| Outcome | Games |", "| --- | --- |"]
        failures.extend(f"| {_escape(_human(outcome))} | {count:,} |" for outcome, count in measured.outcomes.items())
        if len(measured.failure_reasons) > 0:
            failures.extend(["", "| Failure reason | Games |", "| --- | --- |"])
            failures.extend(f"| {_escape(reason)} | {count:,} |" for reason, count in measured.failure_reasons.items())
        if len(measured.responsible_configurations) > 0:
            failures.extend(["", "| Responsible strategy | Games |", "| --- | --- |"])
            failures.extend(
                f"| {_escape(configuration_label(report, config_id))} | {count:,} |"
                for config_id, count in measured.responsible_configurations.items()
            )
        lines.extend(_details("Failure details", failures))
    lines.extend(_details("Measurement notes", ["- " + note for note in measured.resource_notes]))
    return lines


def _rules(report: EvaluationReport) -> str:
    protocol = report.context.protocol
    mode = "Communication" if protocol.communication_enabled else "Classic"
    if protocol.end_condition == "fixed_hands":
        return f"{mode} · {protocol.hands} completed hands"
    return f"{mode} · {report.context.rules.target_score} points, checked after each completed hand"


def _settings(report: EvaluationReport, aliases: dict[str, str]) -> list[str]:
    spec = report.analysis_spec
    lines = [
        "## How to read the results",
        "",
        "Win credit shares one win equally among tied leaders. Top-place credit is the exact chance of making the cutoff after a random tie-break. The two objectives are shown separately; neither is replaced by penalty score.",
        "",
        "Random-lineup results use that random sample alone. Deliberately chosen opponents can contribute to separately weighted results. Replacement comparisons use the same opponents and deals for both candidates.",
        "",
        "Appearances count seats, so two copies of a strategy in one game contribute two appearances. Samples count independent evidence: games sharing a deal or an opponent draw stay together. Both columns show completed / planned counts.",
        "",
        f"Confidence intervals use {spec.bootstrap_samples:,} resamples at the {spec.confidence_level:.0%} level. Each interval covers one estimate; they are not adjusted for examining many comparisons. Evidence label: **{_human(spec.evidence_label)}**.",
        "",
        "**†** A point estimate is available, but its confidence interval is not. **—** No estimate is available because appearances or required opponent combinations are missing. Bounds describe how unfinished or missing outcomes could change a result; they are not confidence intervals.",
        "",
    ]
    lines.extend(
        _details("Coverage and interpretation notes", ["- " + limitation for limitation in report.limitations])
    )
    metadata = [
        "| Setting | Value |",
        "| --- | --- |",
        f"| Analysis version | {_escape(report.analysis_version)} |",
        f"| Resampling seed | {spec.resampling_seed} |",
        f"| Practical effect threshold | {spec.practical_effect_threshold * 100:.1f} percentage points |",
        "",
        "Raw settings and identities below allow this analysis to be reproduced.",
        "",
        "```json",
        spec.model_dump_json(indent=2),
        "```",
        "",
        "| Strategy | Configuration ID |",
        "| --- | --- |",
    ]
    metadata.extend(
        f"| {_escape(configuration_label(report, config_id))} | `{config_id}` |" for config_id in report.catalogue
    )
    if len(aliases) > 0:
        metadata.extend(["", "| Lineup label | Condition ID |", "| --- | --- |"])
        metadata.extend(f"| {label} | `{identity}` |" for identity, label in aliases.items())
    metadata.extend([
        "",
        "### Runtime provenance",
        "",
        "```json",
        json.dumps(report.provenance, indent=2, sort_keys=True),
        "```",
    ])
    lines.extend(_details("Reproducibility settings and identities", metadata))
    return lines


def report_markdown(report: EvaluationReport) -> str:
    """Lead with comparable results; keep detailed evidence available without overwhelming the page."""
    measured = report.diagnostics
    aliases = _conditions(report)
    counts = ", ".join(str(count) for count in report.context.player_counts)
    lines = [
        "# Arena results",
        "",
        f"**{measured.finished_matches:,} of {measured.planned_matches:,} games finished** · {counts} players · {len(report.catalogue)} strategies",
        "",
        _rules(report),
        "",
        f"{measured.started_matches:,} started · {measured.unsuccessful_matches:,} unsuccessful · {measured.missing_matches:,} missing · Run {_human(report.run_status).lower()}",
        "",
        f"Results are percentages. The line below each result is its **{report.analysis_spec.confidence_level:.0%} confidence interval**; **†** means the interval is unavailable and **—** means no estimate. [Reading the results](#how-to-read-the-results) explains ties and coverage.",
        "",
    ]
    lines.extend(_population_sections(report, aliases))
    lines.extend(_comparison_sections(report, aliases))
    lines.extend(_coverage_sections(report, aliases))
    lines.extend(_supporting_results(report, aliases))
    lines.extend(_profiles(report))
    lines.extend(_diagnostics(report))
    used_labels = set(re.findall(r"Lineup [0-9]+", " ".join(lines)))
    lines.extend(_settings(report, {identity: label for identity, label in aliases.items() if label in used_labels}))
    lines.extend([
        "## Saved evidence",
        "",
        "[Structured analysis](analysis.json.gz) · [Run plan](plan.json) · [Returned results](results.jsonl) · [Run manifest](manifest.json)",
        "",
    ])
    return "\n".join(lines)
