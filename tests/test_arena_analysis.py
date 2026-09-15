"""Population estimands and dependency-aware uncertainty from compact records."""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from sixnimmt.analytics import AnalysisSpec, analyse_run, finish_credits, report_markdown, report_terminal
from sixnimmt.analytics.models import Estimate, EvaluationReport
from sixnimmt.analytics.uncertainty import ratio_interval
from sixnimmt.arena.artifacts import ArenaRun, MatchRecord, RunStatus, RuntimeProvenance, load_run, runtime_provenance
from sixnimmt.arena.catalogue import CandidateConfig
from sixnimmt.arena.planning import (
    ArenaPlan,
    CompositionWeight,
    PlannedMatch,
    Population,
    ReplacementComparison,
    ResolvedComparison,
    RunSettings,
    SeatAssignment,
    build_arena_plan,
)
from sixnimmt.arena.results import MatchOutcome


@pytest.fixture
def plan() -> ArenaPlan:
    return build_arena_plan(
        RunSettings(
            catalogue=(CandidateConfig(key="a", bot="lowest_card"), CandidateConfig(key="b", bot="highest_card")),
            player_counts=(4,),
            games=1,
            rotations=False,
            analysis=AnalysisSpec(bootstrap_samples=40, resampling_seed=9),
        )
    )


def _job(plan: ArenaPlan, name: str, lineup: str, **settings: object) -> PlannedMatch:
    ids = {entry.key: entry.config_id for entry in plan.catalogue}
    return PlannedMatch.model_validate(
        {
            "job_id": name,
            "match_id": name,
            "player_count": len(lineup),
            "match_seed": 1,
            "seats": tuple(
                SeatAssignment(seat=seat, config_id=ids[key], instance_id=f"{name}:{seat}", bot_seed=seat)
                for seat, key in enumerate(lineup)
            ),
            "stream": "iid",
            "block_id": name,
            "shared_deal_id": name,
            "population_id": "uniform",
            "condition_id": "condition",
        }
        | settings
    )


def _run(plan: ArenaPlan, jobs: tuple[PlannedMatch, ...], scores: tuple[tuple[int, ...] | None, ...]) -> ArenaRun:
    results = tuple(
        MatchRecord(
            job_id=job.job_id,
            match_id=job.match_id,
            seats=job.seats,
            outcome=MatchOutcome.FINISHED,
            scores=values,
            winners=tuple(seat for seat, value in enumerate(values) if value == min(values)),
            completed_hand_scores=(values,),
        )
        for job, values in zip(jobs, scores, strict=True)
        if values is not None
    )
    return ArenaRun(
        plan=plan.model_copy(update={"jobs": jobs}),
        results=results,
        status=RunStatus(
            state="completed",
            planned_job_ids=tuple(job.job_id for job in jobs),
            started_job_ids=tuple(job.job_id for job in jobs),
            completed_job_ids=tuple(result.job_id for result in results),
        ),
        artifact_dir=Path("saved-comparison"),
    )


def _estimate(report: EvaluationReport, config_id: str, **filters: object) -> Estimate:
    return next(
        estimate
        for estimate in report.population_estimates
        if estimate.configuration_id == config_id
        and estimate.objective == "win_credit"
        and all(getattr(estimate, key) == value for key, value in filters.items())
    )


@pytest.mark.parametrize(
    ("scores", "seat", "cutoff", "win", "acceptable", "distribution"),
    [
        ((0, 0, 4, 6), 0, 2, 0.5, 1, (0.5, 0.5, 0, 0)),
        ((0, 1, 2, 2, 4), 2, 3, 0, 0.5, (0, 0, 0.5, 0.5, 0)),
        ((3, 3, 3, 3), 3, 2, 0.25, 0.5, (0.25, 0.25, 0.25, 0.25)),
    ],
)
def test_exact_ties_distribute_win_and_qualifying_positions(
    scores, seat, cutoff, win, acceptable, distribution
) -> None:
    result = finish_credits(scores, seat, cutoff)
    assert result.win_credit == win
    assert result.acceptable_credit == acceptable
    assert result.finishing_distribution == distribution


def test_iid_estimates_exclude_controlled_extra_sampling(plan: ArenaPlan) -> None:
    jobs = (_job(plan, "i1", "abbb"), _job(plan, "i2", "abbb"), _job(plan, "c1", "abbb", stream="controlled"))
    report = analyse_run(_run(plan, jobs, ((0, 1, 2, 3), (3, 0, 1, 2), (0, 1, 2, 3))))
    estimate = _estimate(report, plan.catalogue[0].config_id, view="iid")
    assert estimate.value == 0.5
    assert estimate.coverage.finished_appearances == 2
    assert estimate.coverage.planned_blocks == 2


def test_missing_outcomes_have_bounds_without_partial_competitive_scores(plan: ArenaPlan) -> None:
    jobs = (_job(plan, "i1", "abbb"), _job(plan, "i2", "abbb"))
    report = analyse_run(_run(plan, jobs, ((0, 1, 2, 3), None)))
    estimate = _estimate(report, plan.catalogue[0].config_id, view="iid")
    assert estimate.value == 1
    assert estimate.missing_outcome_bounds == (0.5, 1)
    assert estimate.coverage.finished_appearances == 1
    assert report.diagnostics.missing_matches == 1


def test_iid_bootstrap_retains_blocks_without_candidate_appearances(plan: ArenaPlan) -> None:
    jobs = (_job(plan, "i1", "abbb"), _job(plan, "i2", "bbbb"), _job(plan, "i3", "bbbb"))
    report = analyse_run(_run(plan, jobs, ((0, 1, 2, 3), (0, 1, 2, 3), (0, 1, 2, 3))))
    estimate = _estimate(report, plan.catalogue[0].config_id, view="iid")
    assert estimate.coverage.sampling_blocks == 3
    assert estimate.coverage.planned_appearances == 1
    assert estimate.interval is None


def test_weighted_population_preserves_unobserved_cells(plan: ArenaPlan) -> None:
    report = analyse_run(_run(plan, (_job(plan, "i1", "abbb"),), ((0, 1, 2, 3),)))
    estimate = _estimate(report, plan.catalogue[0].config_id, view="weighted", population_id="uniform")
    assert estimate.status == "unsupported"
    assert estimate.value is None
    assert sum(cell.weight for cell in estimate.cells) == pytest.approx(1)
    assert any(cell.finished_appearances == 0 and cell.weight > 0 for cell in estimate.cells)


def test_weighted_population_applies_declared_composition_weights(plan: ArenaPlan) -> None:
    a, b = (entry.config_id for entry in plan.catalogue)
    population = Population(
        population_id="group",
        kind="subsets",
        composition_weights=(
            CompositionWeight(player_count=4, opponents=(b, b, b), weight=0.25),
            CompositionWeight(player_count=4, opponents=(a, b, b), weight=0.75),
        ),
    )
    plan = plan.model_copy(update={"populations": (*plan.populations, population)})
    jobs = (_job(plan, "c1", "abbb", stream="controlled"), _job(plan, "c2", "aabb", stream="controlled"))
    report = analyse_run(_run(plan, jobs, ((0, 1, 2, 3), (0, 0, 2, 3))))
    estimate = _estimate(report, a, view="weighted", population_id="group")
    assert estimate.value == pytest.approx(0.625)
    assert estimate.status == "insufficient_data"
    assert estimate.interval is None


def test_reused_lineup_draw_and_duplicate_seats_remain_one_block(plan: ArenaPlan) -> None:
    jobs = (_job(plan, "i1", "aabb", lineup_draw_id="draw"), _job(plan, "i2", "aabb", lineup_draw_id="draw"))
    report = analyse_run(_run(plan, jobs, ((0, 0, 2, 3), (0, 0, 2, 3))))
    estimate = _estimate(report, plan.catalogue[0].config_id, view="iid")
    assert estimate.coverage.finished_appearances == 4
    assert estimate.coverage.finished_blocks == 1
    assert estimate.interval is None


def test_bootstrap_keeps_repeated_draw_multiplicity() -> None:
    interval = ratio_interval({"a": ("iid", 1, 1), "b": ("iid", 0, 1)}, AnalysisSpec(bootstrap_samples=100), "test")
    assert interval is not None
    assert interval.low == 0
    assert interval.high == 1


def test_paired_effect_uses_only_complete_declared_arms(plan: ArenaPlan) -> None:
    a, b = (entry.config_id for entry in plan.catalogue)
    comparison = ResolvedComparison(comparison_id="replacement", reference=b, candidates=(a,), population_id="uniform")
    plan = plan.model_copy(update={"comparisons": (comparison,)})
    jobs = (
        _job(
            plan,
            "a1",
            "abbb",
            stream="matched",
            comparison_id="replacement",
            arm_id=a,
            focal_seat=0,
            block_id="one",
            shared_deal_id="one",
        ),
        _job(
            plan,
            "b1",
            "bbbb",
            stream="matched",
            comparison_id="replacement",
            arm_id=b,
            focal_seat=0,
            block_id="one",
            shared_deal_id="one",
        ),
        _job(
            plan,
            "a2",
            "abbb",
            stream="matched",
            comparison_id="replacement",
            arm_id=a,
            focal_seat=0,
            block_id="two",
            shared_deal_id="two",
        ),
        _job(
            plan,
            "b2",
            "bbbb",
            stream="matched",
            comparison_id="replacement",
            arm_id=b,
            focal_seat=0,
            block_id="two",
            shared_deal_id="two",
        ),
    )
    report = analyse_run(_run(plan, jobs, ((0, 1, 2, 3), (3, 0, 1, 2), (0, 1, 2, 3), None)))
    estimate = next(item for item in report.comparisons if item.objective == "win_credit" and item.condition_id is None)
    assert estimate.value == 1
    assert estimate.coverage.complete_blocks == 1
    assert estimate.coverage.incomplete_pairs == 1
    assert estimate.missing_outcome_bounds == (0, 1)


def test_reports_preserve_evidence_and_unavailable_measurements(plan: ArenaPlan) -> None:
    run = _run(plan, (_job(plan, "i1", "abbb"),), ((0, 1, 2, 3),))
    report = analyse_run(run)
    assert report.analysis_spec.evidence_label == "unspecified"
    assert all(item.evidence_label == "exploratory" for item in report.weakest_cells)
    assert report.diagnostics.decision_seconds_p95 is None
    assert "## Opponent coverage" in report_markdown(report)
    assert "1 planned" in report_terminal(report)
    assert EvaluationReport.model_validate_json(report.model_dump_json()) == report


def test_absent_configurations_remain_visible_in_population_report(plan: ArenaPlan) -> None:
    report = analyse_run(_run(plan, (_job(plan, "i1", "bbbb"),), ((0, 1, 2, 3),)))
    estimate = _estimate(report, plan.catalogue[0].config_id, view="iid")
    assert estimate.value is None
    assert estimate.status == "insufficient_data"
    assert estimate.coverage.planned_appearances == 0
    assert "no planned appearances" in report_terminal(report)


def test_resource_quantiles_pool_actual_call_samples(plan: ArenaPlan) -> None:
    jobs = (_job(plan, "i1", "abbb"), _job(plan, "i2", "abbb"))
    run = _run(plan, jobs, ((0, 1, 2, 3), (0, 1, 2, 3)))
    first = run.results[0].model_copy(
        update={"seat_decision_samples": ((1.0,), (), (), ()), "seat_decision_seconds": (1.0, None, None, None)}
    )
    second = run.results[1].model_copy(
        update={
            "seat_decision_samples": ((9.0, 9.0, 9.0), (), (), ()),
            "seat_decision_seconds": (27.0, None, None, None),
        }
    )
    report = analyse_run(run.model_copy(update={"results": (first, second)}))
    assert report.diagnostics.measured_decision_calls == 4
    assert report.diagnostics.decision_seconds_median == 9
    assert report.diagnostics.decision_seconds_total == 28


def test_finished_hand_penalties_exclude_unfinished_matches(plan: ArenaPlan) -> None:
    jobs = (_job(plan, "i1", "abbb"), _job(plan, "i2", "abbb"))
    run = _run(plan, jobs, ((4, 5, 6, 7), None))
    failed = MatchRecord(
        job_id="i2",
        match_id="i2",
        seats=jobs[1].seats,
        outcome=MatchOutcome.FAILED,
        partial_scores=(90, 90, 90, 90),
        completed_hand_scores=((80, 80, 80, 80),),
    )
    report = analyse_run(run.model_copy(update={"results": (*run.results, failed)}))
    profile = next(
        item
        for item in report.outcome_profiles
        if item.configuration_id == plan.catalogue[0].config_id and item.view == "iid"
    )
    assert profile.finished_hand_penalty == 4
    assert profile.completed_hand_appearances == 1


def test_four_and_five_player_credits_remain_separate(plan: ArenaPlan) -> None:
    jobs = (_job(plan, "i4", "abbb"), _job(plan, "i5", "abbbb"))
    plan = plan.model_copy(update={"player_counts": (4, 5)})
    report = analyse_run(_run(plan, jobs, ((0, 0, 0, 0), (0, 0, 0, 0, 0))))
    estimates = [
        item
        for item in report.population_estimates
        if item.view == "iid"
        and item.configuration_id == plan.catalogue[0].config_id
        and item.objective == "acceptable_credit"
    ]
    assert {item.player_count: item.value for item in estimates} == {4: 0.5, 5: 0.6}


@pytest.mark.parametrize("missing_negative_blocks", [1, 2])
@pytest.mark.parametrize("shared_deals", [False, True])
def test_fixed_background_comparison_keeps_population_weights_after_failures(
    plan: ArenaPlan, missing_negative_blocks: int, shared_deals: bool
) -> None:
    a, b = (entry.config_id for entry in plan.catalogue)
    population = Population(
        population_id="backgrounds",
        kind="subsets",
        composition_weights=(
            CompositionWeight(player_count=4, opponents=(b, b, b), weight=0.5),
            CompositionWeight(player_count=4, opponents=(a, b, b), weight=0.5),
        ),
    )
    comparison = ResolvedComparison(
        comparison_id="replacement", reference=b, candidates=(a,), population_id="backgrounds"
    )
    plan = plan.model_copy(update={"comparisons": (comparison,), "populations": (*plan.populations, population)})
    jobs: list[PlannedMatch] = []
    scores: list[tuple[int, ...] | None] = []
    for condition, candidate_lineup, reference_lineup in (("positive", "abbb", "bbbb"), ("negative", "aabb", "babb")):
        for block in range(2):
            block_id = f"shared-{block}" if shared_deals else f"{condition}-{block}"
            for arm, lineup in ((a, candidate_lineup), (b, reference_lineup)):
                job = _job(
                    plan,
                    f"{condition}-{block_id}-{arm}",
                    lineup,
                    stream="matched",
                    comparison_id="replacement",
                    arm_id=arm,
                    focal_seat=0,
                    block_id=block_id,
                    shared_deal_id=block_id,
                    condition_id=condition,
                    population_id="backgrounds",
                )
                jobs.append(job)
                wins = (condition == "positive" and arm == a) or (condition == "negative" and arm == b)
                result = (0, 1, 2, 3) if wins else (3, 0, 1, 2)
                scores.append(
                    None if condition == "negative" and arm == b and block < missing_negative_blocks else result
                )
    report = analyse_run(_run(plan, tuple(jobs), tuple(scores)))
    estimate = next(item for item in report.comparisons if item.objective == "win_credit" and item.condition_id is None)
    if missing_negative_blocks == 1:
        assert estimate.value == 0
        assert estimate.status == "insufficient_data"
    else:
        assert estimate.value is None
        assert estimate.status == "unsupported"
    assert tuple(cell.weight for cell in estimate.cells) == (0.5, 0.5)
    if shared_deals:
        difference = next(item for item in report.condition_effects if item.objective == "win_credit")
        assert difference.coverage.planned_blocks == 2
        assert difference.coverage.complete_blocks == 2 - missing_negative_blocks
        assert difference.value == (-2 if missing_negative_blocks == 1 else None)


def test_unstarted_shared_conditions_preserve_planned_coverage_without_claiming_started_blocks() -> None:
    plan = build_arena_plan(
        RunSettings(
            catalogue=(CandidateConfig(key="a", bot="lowest_card"), CandidateConfig(key="b", bot="highest_card")),
            player_counts=(4,),
            games=0,
            share_controlled_deals=True,
            comparisons=(
                ReplacementComparison(
                    comparison_id="replacement",
                    reference="a",
                    candidates=("b",),
                    backgrounds=(("a", "a", "a"), ("a", "a", "b")),
                    games=2,
                ),
            ),
        )
    )
    run = _run(plan, plan.jobs, tuple(None for _ in plan.jobs))
    status = RunStatus(
        state="stopped", planned_job_ids=run.status.planned_job_ids, unstarted_job_ids=run.status.planned_job_ids
    )
    report = analyse_run(run.model_copy(update={"status": status}))
    assert len(report.condition_effects) == 2
    for difference in report.condition_effects:
        assert difference.coverage.planned_blocks == 2
        assert difference.coverage.started_appearances == 0
        assert difference.coverage.complete_blocks == 0
        assert difference.missing_outcome_bounds == (-2, 2)
        assert difference.cells == ()


@pytest.mark.parametrize("player_count", range(2, 11))
def test_default_qualifying_positions_cover_all_supported_player_counts(plan: ArenaPlan, player_count: int) -> None:
    plan = plan.model_copy(update={"player_counts": (player_count,)})
    job = _job(plan, "game", "a" + "b" * (player_count - 1))
    report = analyse_run(_run(plan, (job,), (tuple(0 for _ in range(player_count)),)))
    qualifying = next(
        item
        for item in report.population_estimates
        if item.view == "iid"
        and item.configuration_id == plan.catalogue[0].config_id
        and item.objective == "acceptable_credit"
    )
    assert qualifying.value == pytest.approx(((player_count + 1) // 2) / player_count)
    assert _estimate(report, plan.catalogue[0].config_id, view="iid").value == pytest.approx(1 / player_count)


def test_large_population_keeps_missing_probability_without_listing_unplayed_combinations() -> None:
    plan = build_arena_plan(RunSettings(player_counts=(10,), games=1, analysis=AnalysisSpec(bootstrap_samples=20)))
    results = tuple(tuple(0 for _ in job.seats) for job in plan.jobs)
    report = analyse_run(_run(plan, plan.jobs, results))
    estimates = [
        item for item in report.population_estimates if item.view == "weighted" and item.population_id == "uniform"
    ]
    assert len(estimates) == 2 * len(plan.catalogue)
    for estimate in estimates:
        assert estimate.status == "unsupported"
        assert estimate.value is None
        assert estimate.target_cell_count == 92378
        assert len(estimate.cells) <= 10
        assert estimate.unlisted_cell_count == 92378 - len(estimate.cells)
        assert sum(cell.weight for cell in estimate.cells) + estimate.unlisted_cell_weight == pytest.approx(1)
        assert estimate.missing_outcome_bounds is not None
        assert estimate.missing_outcome_bounds[1] >= estimate.unlisted_cell_weight
    markdown = report_markdown(report)
    assert "unplayed combinations are not listed individually" in markdown
    assert len(markdown) < 100000


def test_reports_explain_independent_samples_without_internal_scheduling_terms(plan: ArenaPlan) -> None:
    report = analyse_run(_run(plan, (_job(plan, "game", "abbb"),), ((0, 1, 2, 3),)))
    terminal = report_terminal(report)
    markdown = report_markdown(report)
    assert "Independent samples" in terminal
    assert "blocks" not in terminal.lower()
    assert "blocks" not in markdown.lower()
    assert "random lineups" in terminal


def test_markdown_combines_objectives_and_keeps_raw_configuration_ids_out_of_main_results(plan: ArenaPlan) -> None:
    report = analyse_run(_run(plan, (_job(plan, "game", "abbb"),), ((0, 1, 2, 3),)))
    markdown = report_markdown(report)
    main_results = markdown.split("## Opponent coverage")[0]
    assert "| Strategy | Win | Top 2 | Avg score |" in main_results
    assert main_results.count("| A |") == 1
    assert "**100.0%** †" in main_results
    assert "config-" not in main_results
    assert "```text" not in markdown
    assert "interval unavailable; insufficient data" not in markdown
    assert "[Structured analysis](analysis.json.gz)" in markdown


def test_markdown_bounds_large_detail_previews_and_links_complete_results(plan: ArenaPlan) -> None:
    report = analyse_run(_run(plan, (_job(plan, "game", "abbb"),), ((0, 1, 2, 3),)))
    focal = tuple(item for item in report.seat_order_estimates if item.configuration_id == plan.catalogue[0].config_id)
    detailed = tuple(
        item.model_copy(update={"condition_id": f"setting-{index}"}) for index in range(50) for item in focal
    )
    report = report.model_copy(update={"seat_order_estimates": detailed})
    markdown = report_markdown(report)
    assert "Showing 40 of 50 settings" in markdown
    assert "[Full results](analysis.json.gz)" in markdown
    assert len(report.seat_order_estimates) == 100
    assert "Setting-49" not in markdown


@pytest.mark.parametrize("mismatch", ["match_id", "seats"])
def test_analysis_rejects_result_identity_mismatches(plan: ArenaPlan, mismatch: str) -> None:
    job = _job(plan, "game", "abbb")
    run = _run(plan, (job,), ((0, 1, 2, 3),))
    record = run.results[0]
    invalid = record.model_copy(
        update={"match_id": "another-game"} if mismatch == "match_id" else {"seats": tuple(reversed(record.seats))}
    )
    with pytest.raises(ValueError, match="result identities do not match"):
        analyse_run(run.model_copy(update={"results": (invalid,)}))


def test_analysis_counts_committed_results_when_execution_status_is_stale(plan: ArenaPlan) -> None:
    job = _job(plan, "game", "abbb")
    run = _run(plan, (job,), ((0, 1, 2, 3),))
    stale = RunStatus(state="running", planned_job_ids=(job.job_id,), unstarted_job_ids=(job.job_id,))
    report = analyse_run(run.model_copy(update={"status": stale}))
    assert report.diagnostics.started_matches == 1
    estimate = _estimate(report, plan.catalogue[0].config_id, view="iid")
    assert estimate.coverage.started_appearances == 1


def test_reports_label_every_candidate_in_a_multi_candidate_comparison() -> None:
    plan = build_arena_plan(
        RunSettings(
            catalogue=(
                CandidateConfig(bot="lowest_card", key="reference", label="Reference"),
                CandidateConfig(bot="highest_card", key="first", label="Candidate"),
                CandidateConfig(bot="random", key="second", label="Candidate"),
            ),
            player_counts=(3,),
            games=0,
            rotations=False,
            comparisons=(
                ReplacementComparison(
                    comparison_id="replacement",
                    reference="reference",
                    candidates=("first", "second"),
                    backgrounds=(("reference", "reference"),),
                    games=2,
                ),
            ),
            analysis=AnalysisSpec(bootstrap_samples=0),
        )
    )
    report = analyse_run(_run(plan, plan.jobs, tuple((0, 1, 2) for _ in plan.jobs)))
    terminal = report_terminal(report, width=140)
    markdown = report_markdown(report)
    for candidate in plan.catalogue[1:]:
        label = f"Candidate ({candidate.config_id[-6:]}) vs Reference"
        assert label in terminal
        assert label in markdown
    assert terminal.count("completed comparisons") == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"seat_stats": ({"nested": {"value": float("nan")}}, None, None, None)},
        {"seat_stats": ({"value": object()}, None, None, None)},
        {"seat_decision_calls": (True, 0, 0, 0)},
        {"seat_decision_seconds": (-1.0, None, None, None)},
    ],
)
def test_compact_evidence_rejects_invalid_measurements(plan: ArenaPlan, changes: dict[str, object]) -> None:
    job = _job(plan, "game", "abbb")
    run = _run(plan, (job,), ((0, 1, 2, 3),))
    payload = run.results[0].model_dump(mode="python")
    with pytest.raises(ValueError):
        MatchRecord.model_validate(payload | changes)


def test_reports_disambiguate_labels_that_normalize_to_the_same_display(plan: ArenaPlan) -> None:
    report = analyse_run(_run(plan, (_job(plan, "game", "abbb"),), ((0, 1, 2, 3),)))
    first, second = (entry.config_id for entry in plan.catalogue)
    report = report.model_copy(update={"catalogue": {first: "same_label", second: "Same label"}})
    for text in (report_markdown(report), report_terminal(report)):
        assert f"Same label ({first[-6:]})" in text
        assert f"Same label ({second[-6:]})" in text


def test_terminal_report_does_not_advertise_unpublished_analysis_files(plan: ArenaPlan) -> None:
    report = analyse_run(_run(plan, (_job(plan, "game", "abbb"),), ((0, 1, 2, 3),)))
    terminal = report_terminal(report)
    assert "Full report" not in terminal
    assert "Saved data" not in terminal
    assert "See the saved report" not in terminal


@pytest.fixture(params=["plan", "record", "status", "provenance"])
def versioned_model(request: pytest.FixtureRequest, plan: ArenaPlan) -> tuple[BaseModel, str]:
    if request.param == "plan":
        return plan, "version"
    if request.param == "record":
        job = plan.jobs[0]
        return MatchRecord(
            job_id=job.job_id, match_id=job.match_id, seats=job.seats, outcome="failed"
        ), "record_version"
    if request.param == "status":
        return RunStatus(state="running", planned_job_ids=()), "status_version"
    return RuntimeProvenance.model_validate(runtime_provenance(plan)), "provenance_version"


@pytest.mark.parametrize("version", [True, False, 1.0, "1", 2])
def test_evidence_schema_versions_require_exact_supported_integer(
    versioned_model: tuple[BaseModel, str], version: object
) -> None:
    model, field = versioned_model
    payload = model.model_dump(mode="json")
    payload[field] = version
    with pytest.raises(ValidationError):
        type(model).model_validate_json(json.dumps(payload))


def test_evidence_schema_integer_version_roundtrips(versioned_model: tuple[BaseModel, str]) -> None:
    model, field = versioned_model
    restored = type(model).model_validate_json(model.model_dump_json())
    assert type(getattr(restored, field)) is int
    assert getattr(restored, field) == 1
    assert restored == model


@pytest.mark.parametrize("field", ["manifest_version", "provenance_version"])
@pytest.mark.parametrize("version", [True, False, 1.0, "1", 2])
def test_saved_run_rejects_invalid_manifest_or_provenance_version(
    tmp_path: Path, plan: ArenaPlan, field: str, version: object
) -> None:
    status = RunStatus(state="running", planned_job_ids=tuple(job.job_id for job in plan.jobs))
    manifest: dict[str, object] = {
        "manifest_version": 1,
        "artifact_kind": "arena_run",
        "plan_id": plan.plan_id,
        "status": status.model_dump(mode="json"),
        "provenance": {},
    }
    if field == "manifest_version":
        manifest[field] = version
    else:
        manifest["provenance"] = {field: version}
    (tmp_path / "plan.json").write_text(plan.model_dump_json())
    (tmp_path / "results.jsonl").write_text("")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValidationError):
        load_run(tmp_path)


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_in_memory_evidence_rejects_coerced_provenance_version(plan: ArenaPlan, version: object) -> None:
    run = _run(plan, plan.jobs, ((0, 1, 2, 3),))
    payload = run.model_dump(mode="json")
    payload["provenance"] = {"provenance_version": version}
    with pytest.raises(ValidationError):
        ArenaRun.model_validate(payload)
