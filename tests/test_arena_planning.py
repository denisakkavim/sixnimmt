"""Observable guarantees of deterministic population and comparison schedules."""

import json
from collections import Counter, defaultdict
from pathlib import Path

import pytest
from pydantic import ValidationError

from sixnimmt.arena.bots.registry import REGISTRY
from sixnimmt.arena.catalogue import (
    CandidateConfig,
    reference_catalogue,
    resolve_catalogue,
    resolve_frozen_catalogue,
    validate_frozen_catalogue,
)
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.planning import (
    ArenaPlan,
    PlannedMatch,
    PopulationConfig,
    ReplacementComparison,
    RunSettings,
    build_arena_plan,
    with_execution,
)
from sixnimmt.arena.players import PlayerConfig
from sixnimmt.common.evaluation import AnalysisSpec
from sixnimmt.engine.rules import EndCondition, MatchProtocol


@pytest.fixture
def small_catalogue() -> tuple[CandidateConfig, ...]:
    return (
        CandidateConfig(bot="random", key="random", family="random"),
        CandidateConfig(bot="lowest_card", key="low", family="ordered"),
        CandidateConfig(bot="highest_card", key="high", family="ordered"),
    )


def test_reference_catalogue_freezes_all_eleven_configurations_and_nested_defaults() -> None:
    catalogue = resolve_catalogue(reference_catalogue())
    assert len(catalogue) == 11
    burn = next(entry for entry in catalogue if entry.bot == "controlled_burn")
    assert burn.options == {"K": 5, "fallback_strategy": "closest_gap", "fallback_options": {}}
    assert burn.implementation_id.startswith("implementation-")
    assert burn.config_id.startswith("config-")
    assert burn.deterministic


def test_configuration_identity_ignores_labels_and_resolves_equivalent_defaults() -> None:
    original = CandidateConfig(bot="controlled_burn", options={"K": 5, "fallback_strategy": "closest_gap"})
    explicit = CandidateConfig(
        bot="controlled_burn",
        key="different",
        label="Renamed",
        family="different",
        options={"K": 5, "fallback_strategy": "closest_gap", "fallback_options": {}},
    )
    changed = CandidateConfig(bot="controlled_burn", options={"K": 6, "fallback_strategy": "closest_gap"})
    assert resolve_catalogue((original,))[0].config_id == resolve_catalogue((explicit,))[0].config_id
    assert resolve_catalogue((original,))[0].config_id != resolve_catalogue((changed,))[0].config_id


def test_nested_composed_defaults_are_resolved_recursively() -> None:
    entry = resolve_catalogue((
        CandidateConfig(
            bot="hand_aware_row_choice",
            options={
                "max_extra_penalty": 2,
                "card_strategy": "controlled_burn",
                "card_options": {"K": 5, "fallback_strategy": "closest_gap"},
            },
        ),
    ))[0]
    assert entry.options["card_options"] == {"K": 5, "fallback_strategy": "closest_gap", "fallback_options": {}}


def test_catalogue_rejects_duplicate_keys_and_equivalent_configurations() -> None:
    with pytest.raises(ValueError, match="duplicate catalogue key"):
        resolve_catalogue((CandidateConfig(bot="random"), CandidateConfig(bot="random")))
    with pytest.raises(ValueError, match="duplicate resolved configuration"):
        resolve_catalogue((CandidateConfig(bot="random", key="a"), CandidateConfig(bot="random", key="b")))


def test_execution_settings_change_plan_identity_without_changing_jobs(
    small_catalogue: tuple[CandidateConfig, ...],
) -> None:
    original = build_arena_plan(RunSettings(catalogue=small_catalogue, games=1))
    changed = with_execution(original, RunConfig(concurrency=3))
    assert changed.plan_id != original.plan_id
    assert changed.jobs == original.jobs
    assert changed.execution.concurrency == 3
    assert changed.execution.max_abandoned_decisions == 12
    assert changed.design["execution"] == json.loads(changed.model_dump_json())["execution"]
    assert with_execution(changed, changed.execution) == changed


def test_saved_catalogue_rejects_changed_registered_factory(monkeypatch) -> None:
    catalogue = resolve_catalogue((CandidateConfig(bot="lowest_card"),))
    assert len(resolve_frozen_catalogue(catalogue)) == 1
    monkeypatch.setitem(REGISTRY, "lowest_card", REGISTRY["highest_card"])
    with pytest.raises(ValueError, match="current implementation"):
        resolve_frozen_catalogue(catalogue)


def test_resolved_plan_is_deeply_immutable_and_roundtrips_json(
    small_catalogue: tuple[CandidateConfig, ...],
) -> None:
    plan = build_arena_plan(RunSettings(catalogue=small_catalogue, games=1, execution=RunConfig(concurrency=2)))
    restored = ArenaPlan.model_validate_json(plan.model_dump_json())
    assert restored == plan
    restored.catalogue[0].options["injected"] = 3
    restored.catalogue[0].metadata["injected"] = 3
    restored.design["games"] = 500
    assert "injected" not in restored.catalogue[0].options
    assert "injected" not in restored.catalogue[0].metadata
    assert restored.design["games"] == 1
    with pytest.raises(ValidationError, match="frozen"):
        restored.seed = 0


def test_run_settings_require_the_common_recording_options(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match=r"use recording\.output_dir and recording\.trace"):
        RunSettings.model_validate({"execution": {"trace_dir": str(tmp_path)}})


def test_iid_samples_complete_lineups_with_replacement(small_catalogue: tuple[CandidateConfig, ...]) -> None:
    plan = build_arena_plan(RunSettings(catalogue=small_catalogue, player_counts=(4,), games=100, rotations=False))
    assert len(plan.jobs) == 100
    assert len({job.block_id for job in plan.jobs}) == 100
    assert all(job.stream == "iid" for job in plan.jobs)
    assert all(job.selection_probability == pytest.approx(1 / 3**4) for job in plan.jobs)
    assert any(len(set(job.lineup)) == 1 for job in plan.jobs)
    assert any(len(set(job.lineup)) == 3 for job in plan.jobs)
    appearances = Counter(key for job in plan.jobs for key in job.lineup)
    assert set(appearances) == {entry.config_id for entry in plan.catalogue}
    assert max(appearances.values()) - min(appearances.values()) < 70


def test_each_random_game_has_a_fresh_lineup_draw_deal_and_private_seeds(
    small_catalogue: tuple[CandidateConfig, ...],
) -> None:
    plan = build_arena_plan(RunSettings(catalogue=small_catalogue, player_counts=(4,), games=6))
    assert len(plan.jobs) == 6
    assert len({job.lineup_draw_id for job in plan.jobs}) == 6
    assert len({job.block_id for job in plan.jobs}) == 6
    assert len({job.match_seed for job in plan.jobs}) == 6
    assert len({job.shared_deal_id for job in plan.jobs}) == 6
    assert len({seat.bot_seed for job in plan.jobs for seat in job.seats}) == 24


@pytest.mark.parametrize(("players", "compositions"), [(4, 12), (5, 15)])
def test_pair_coverage_includes_every_copy_count_once(
    small_catalogue: tuple[CandidateConfig, ...], players, compositions
) -> None:
    plan = build_arena_plan(
        RunSettings(catalogue=small_catalogue, player_counts=(players,), games=0, controlled_games=2)
    )
    conditions = Counter(job.condition_id for job in plan.jobs)
    assert len(conditions) == compositions
    assert set(conditions.values()) == {2}
    assert len(plan.jobs) == compositions * 2
    assert all(job.planned_quota == 2 and job.selection_probability is None for job in plan.jobs)
    observed = {tuple(sorted(Counter(job.lineup).values())) for job in plan.jobs}
    assert observed == {tuple(sorted((copies, players - copies))) for copies in range(1, players)} | {(players,)}


@pytest.mark.parametrize(("players", "compositions"), [(4, 1001), (5, 3003)])
def test_exhaustive_reference_coverage_counts_distinct_multisets(players, compositions) -> None:
    plan = build_arena_plan(
        RunSettings(
            player_counts=(players,),
            games=0,
            controlled_games=1,
            controlled_coverage="exhaustive",
            rotations=False,
        )
    )
    assert len(plan.jobs) == compositions
    assert len({job.condition_id for job in plan.jobs}) == compositions


def test_explicit_compositions_deduplicate_seat_order(small_catalogue: tuple[CandidateConfig, ...]) -> None:
    plan = build_arena_plan(
        RunSettings(
            catalogue=small_catalogue,
            player_counts=(4,),
            games=0,
            controlled_games=1,
            controlled_coverage="explicit",
            compositions=(("low", "low", "high", "random"), ("random", "low", "high", "low")),
        )
    )
    assert len(plan.jobs) == 1
    low = next(entry.config_id for entry in plan.catalogue if entry.key == "low")
    assert all(Counter(job.lineup)[low] == 2 for job in plan.jobs)
    for job in plan.jobs:
        low_seat = next(seat.seat for seat in job.seats if seat.config_id == low)
        assert Counter(job.opponent_composition(low_seat))[low] == 1


def test_controlled_rotations_balance_seats_across_fresh_games(
    small_catalogue: tuple[CandidateConfig, ...],
) -> None:
    plan = build_arena_plan(
        RunSettings(
            catalogue=small_catalogue,
            player_counts=(5,),
            games=0,
            controlled_games=5,
            controlled_coverage="explicit",
            compositions=(("random", "random", "random", "low", "high"),),
        )
    )
    low = next(entry.config_id for entry in plan.catalogue if entry.key == "low")
    positions = [seat.seat for job in plan.jobs for seat in job.seats if seat.config_id == low]
    assert sorted(positions) == list(range(5))
    assert len({job.match_seed for job in plan.jobs}) == 5
    assert len({seat.instance_id for job in plan.jobs for seat in job.seats}) == 25
    assert len({seat.bot_seed for job in plan.jobs for seat in job.seats}) == 25


def test_schedule_is_deterministic_and_root_seed_changes_fresh_games(
    small_catalogue: tuple[CandidateConfig, ...],
) -> None:
    config = RunSettings(
        catalogue=small_catalogue,
        player_counts=(4,),
        games=5,
        controlled_games=8,
        reverse_order=True,
        additional_permutations=2,
    )
    first = build_arena_plan(config)
    assert first == build_arena_plan(config)
    changed = build_arena_plan(config.model_copy(update={"seed": 99}))
    assert {job.match_seed for job in first.jobs}.isdisjoint(job.match_seed for job in changed.jobs)
    assert {seat.bot_seed for job in first.jobs for seat in job.seats}.isdisjoint(
        seat.bot_seed for job in changed.jobs for seat in job.seats
    )
    assert [job.job_id for job in first.jobs] != sorted(job.job_id for job in first.jobs)


@pytest.mark.parametrize("count", range(2, 11))
def test_game_budget_is_exact_for_every_supported_player_count(
    small_catalogue: tuple[CandidateConfig, ...],
    count: int,
) -> None:
    plan = build_arena_plan(
        RunSettings(
            catalogue=small_catalogue, player_counts=(count,), games=7, reverse_order=True, additional_permutations=3
        )
    )
    assert len(plan.jobs) == 7
    assert all(len(job.seats) == count for job in plan.jobs)
    assert len({job.match_seed for job in plan.jobs}) == 7


@pytest.mark.parametrize("count", (1, 11, True))
def test_rejects_player_counts_outside_engine_support(count: int) -> None:
    with pytest.raises(ValidationError):
        RunSettings(player_counts=(count,))


def test_default_schedule_uses_one_player_count_and_the_declared_game_budget() -> None:
    plan = build_arena_plan(RunSettings(games=3))
    assert plan.player_counts == (4,)
    assert len(plan.jobs) == 3


def test_named_populations_preserve_family_weights_and_member_subsets(
    small_catalogue: tuple[CandidateConfig, ...],
) -> None:
    plan = build_arena_plan(
        RunSettings(
            catalogue=small_catalogue,
            player_counts=(4,),
            games=1,
            population_id="ordered_only",
            populations=(PopulationConfig(population_id="ordered_only", members=("low", "high")),),
        )
    )
    families = next(population for population in plan.populations if population.population_id == "family_balanced")
    keys = {entry.config_id: entry.key for entry in plan.catalogue}
    assert {keys[item.config_id]: item.weight for item in families.weights} == {
        "random": 0.5,
        "low": 0.25,
        "high": 0.25,
    }
    assert all(keys[key] in {"low", "high"} for job in plan.jobs for key in job.lineup)
    assert all(job.population_id == "ordered_only" for job in plan.jobs)


def test_matched_replacements_keep_backgrounds_slots_and_seeds_identical(
    small_catalogue: tuple[CandidateConfig, ...],
) -> None:
    plan = build_arena_plan(
        RunSettings(
            catalogue=small_catalogue,
            player_counts=(4,),
            games=0,
            reverse_order=True,
            additional_permutations=2,
            comparisons=(
                ReplacementComparison(
                    comparison_id="ordered",
                    reference="low",
                    candidates=("high",),
                    backgrounds=(("low", "random", "random"),),
                    games=16,
                ),
            ),
        )
    )
    pairs: dict[tuple[str, int, int], list[PlannedMatch]] = defaultdict(list)
    for job in plan.jobs:
        pairs[(job.shared_deal_id, job.rotation, job.permutation)].append(job)
    assert len(pairs) == 16
    assert len(plan.jobs) == 32
    assert {job.permutation for job in plan.jobs} == {0, 1, 2, 3}
    for jobs in pairs.values():
        assert len(jobs) == 2
        first, second = jobs
        assert first.focal_seat == second.focal_seat
        assert first.focal_seat is not None
        assert second.focal_seat is not None
        assert first.match_seed == second.match_seed
        assert first.block_id == second.block_id
        assert first.opponent_composition(first.focal_seat) == second.opponent_composition(second.focal_seat)
        assert tuple(seat.bot_seed for seat in first.seats) == tuple(seat.bot_seed for seat in second.seats)
        for a, b in zip(first.seats, second.seats, strict=True):
            assert (a.config_id != b.config_id) == (a.seat == first.focal_seat)


def test_group_roster_expands_all_subsets_with_declared_weights() -> None:
    roster = ("random", "lowest_card", "highest_card", "lowest_fitting_card", "highest_fitting_card", "closest_gap")
    plan = build_arena_plan(
        RunSettings(
            player_counts=(4, 5),
            games=0,
            rotations=False,
            comparisons=(
                ReplacementComparison(
                    comparison_id="group",
                    reference="coldest_row",
                    candidates=("hand_flexibility",),
                    group_roster=roster,
                ),
            ),
        )
    )
    population = next(population for population in plan.populations if population.kind == "subsets")
    assert len([cell for cell in population.composition_weights if cell.player_count == 4]) == 20
    assert len([cell for cell in population.composition_weights if cell.player_count == 5]) == 15
    assert len(plan.jobs) == 70
    assert all(len(set(cell.opponents)) == len(cell.opponents) for cell in population.composition_weights)


def test_sampled_replacement_backgrounds_preserve_draw_identity(small_catalogue: tuple[CandidateConfig, ...]) -> None:
    plan = build_arena_plan(
        RunSettings(
            catalogue=small_catalogue,
            player_counts=(4,),
            games=0,
            comparisons=(
                ReplacementComparison(
                    comparison_id="random_background",
                    reference="low",
                    candidates=("high",),
                    background_population="uniform",
                    background_draws=3,
                    games=2,
                ),
            ),
        )
    )
    assert len(plan.jobs) == 3 * 2 * 2
    assert len({job.lineup_draw_id for job in plan.jobs}) == 3
    assert len({job.block_id for job in plan.jobs}) == 3
    assert len({job.shared_deal_id for job in plan.jobs}) == 6


def test_shared_controlled_deals_explicitly_share_blocks(small_catalogue: tuple[CandidateConfig, ...]) -> None:
    plan = build_arena_plan(
        RunSettings(
            catalogue=small_catalogue,
            player_counts=(4,),
            games=0,
            controlled_games=2,
            rotations=False,
            share_controlled_deals=True,
        )
    )
    assert len({job.condition_id for job in plan.jobs}) == 12
    assert len({job.block_id for job in plan.jobs}) == 2
    assert len({job.shared_deal_id for job in plan.jobs}) == 2
    assert len({job.match_seed for job in plan.jobs}) == 2


def test_protocol_json_defaults_to_anonymous_names(small_catalogue: tuple[CandidateConfig, ...]) -> None:
    config = RunSettings.model_validate_json(
        json.dumps({
            "catalogue": [entry.model_dump() for entry in small_catalogue],
            "games": 1,
            "protocol": {"end_condition": "fixed_hands", "hands": 1},
        })
    )
    assert config.protocol.anonymise_display_names
    assert config.protocol.end_condition == EndCondition.FIXED_HANDS
    explicit = RunSettings(catalogue=small_catalogue, games=1, protocol=MatchProtocol())
    assert explicit.protocol.anonymise_display_names


@pytest.mark.parametrize("corruption", ["duplicate_job", "unknown_configuration", "unsafe_match_id", "unknown_count"])
def test_saved_plan_rejects_inconsistent_job_contract(
    small_catalogue: tuple[CandidateConfig, ...], corruption: str
) -> None:
    plan = build_arena_plan(RunSettings(catalogue=small_catalogue, player_counts=(4,), games=1))
    data = json.loads(plan.model_dump_json())
    if corruption == "duplicate_job":
        data["jobs"].append(data["jobs"][0])
    elif corruption == "unknown_configuration":
        data["jobs"][0]["seats"][0]["config_id"] = "missing"
    elif corruption == "unsafe_match_id":
        data["jobs"][0]["match_id"] = "../../outside"
    else:
        data["player_counts"] = [5]
    with pytest.raises(ValidationError):
        ArenaPlan.model_validate(data)


@pytest.mark.parametrize(
    "settings",
    [
        {"compositions": (("unknown", "low", "high", "low"),)},
        {"compositions": (("low", "high"),)},
        {"population_id": "missing"},
        {"populations": (PopulationConfig(population_id="broken", weighting="explicit", weights={"low": -1}),)},
    ],
)
def test_rejects_invalid_lineup_references(small_catalogue: tuple[CandidateConfig, ...], settings) -> None:
    with pytest.raises(ValueError):
        build_arena_plan(RunSettings(catalogue=small_catalogue, player_counts=(4,), games=1, **settings))


@pytest.mark.arena_slow
def test_varied_population_schedule_preserves_game_budgets_and_seed_relationships_at_volume(
    small_catalogue: tuple[CandidateConfig, ...],
) -> None:
    config = RunSettings(
        catalogue=small_catalogue,
        player_counts=tuple(range(2, 11)),
        games=100,
        controlled_games=8,
        reverse_order=True,
        additional_permutations=3,
        comparisons=(
            ReplacementComparison(
                comparison_id="ordered",
                reference="low",
                candidates=("high",),
                background_population="uniform",
                background_draws=3,
                games=8,
            ),
        ),
    )
    plan = build_arena_plan(config)
    assert plan == build_arena_plan(config)
    assert len(plan.jobs) == 2628
    for count in range(2, 11):
        counts = Counter(job.stream for job in plan.jobs if job.player_count == count)
        assert counts == {"iid": 100, "controlled": 24 * count, "matched": 48}
    assert len({job.job_id for job in plan.jobs}) == len(plan.jobs)
    deals: dict[str, list[PlannedMatch]] = defaultdict(list)
    for job in plan.jobs:
        deals[job.shared_deal_id].append(job)
    for jobs in deals.values():
        if jobs[0].stream == "matched":
            assert len(jobs) == 2
            assert jobs[0].match_seed == jobs[1].match_seed
            assert tuple(seat.bot_seed for seat in jobs[0].seats) == tuple(seat.bot_seed for seat in jobs[1].seats)
        else:
            assert len(jobs) == 1


@pytest.mark.parametrize("design", [[], 1, None, '"text"', "[]"])
def test_plan_rejects_non_object_design(small_catalogue: tuple[CandidateConfig, ...], design: object) -> None:
    plan = build_arena_plan(RunSettings(catalogue=small_catalogue, games=1))
    payload = plan.model_dump(mode="json")
    payload["design"] = design
    with pytest.raises(ValidationError, match="plan design must be a JSON object"):
        ArenaPlan.model_validate(payload)


@pytest.mark.parametrize("setting", ["games", "controlled_games", "additional_permutations"])
@pytest.mark.parametrize("value", [True, "2", 1.5])
def test_lineup_rejects_non_integer_counts(setting: str, value: object) -> None:
    with pytest.raises(ValidationError):
        RunSettings.model_validate({setting: value})


@pytest.mark.parametrize(
    "settings", [{"bootstrap_samples": True}, {"streams": ["invented"]}, {"player_counts": [True]}]
)
def test_analysis_rejects_invalid_counts_and_unknown_streams(settings: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AnalysisSpec.model_validate(settings)


def test_fixed_settings_derive_one_game_and_keep_named_seats() -> None:
    settings = RunSettings(
        catalogue=(CandidateConfig(bot="lowest_card", key="low", label="Low"),),
        lineup=("low", "low"),
    )
    assert settings.games == 1
    assert settings.player_counts == (2,)
    assert not settings.protocol.anonymise_display_names
    assert tuple(player.display_name for player in settings.players()) == ("Low", "Low")


def test_fixed_plan_preserves_order_and_repeats_with_fresh_seeds() -> None:
    settings = RunSettings(
        catalogue=(CandidateConfig(bot="lowest_card", key="low"), CandidateConfig(bot="highest_card", key="high")),
        lineup=("high", "low", "high"),
        games=4,
    )
    plan = build_arena_plan(settings)
    identities = {entry.key: entry.config_id for entry in plan.catalogue}
    assert len(plan.jobs) == 4
    assert all(job.lineup == tuple(identities[key] for key in settings.lineup) for job in plan.jobs)
    assert all(job.stream == "fixed" and job.rotation == job.permutation == 0 for job in plan.jobs)
    assert len({job.match_seed for job in plan.jobs}) == 4
    assert len({seat.bot_seed for job in plan.jobs for seat in job.seats}) == 12
    assert plan.populations == ()
    assert plan.comparisons == ()
    assert plan == build_arena_plan(settings)


@pytest.mark.parametrize("changes", [{"rotations": True}, {"controlled_games": 1}, {"player_counts": [4]}])
def test_fixed_settings_reject_conflicting_schedule_settings(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RunSettings.model_validate({"lineup": ["random", "random"]} | changes)


def test_fixed_external_plan_omits_private_command_options_and_verifies_originals() -> None:
    settings = RunSettings(
        catalogue=(
            CandidateConfig(bot="command", key="custom", options={"command": ["example", "private-argument"]}),
            CandidateConfig(bot="lowest_card", key="low"),
        ),
        lineup=("custom", "low"),
    )
    plan = build_arena_plan(settings)
    external = next(entry for entry in plan.catalogue if entry.key == "custom")
    assert "private-argument" not in plan.model_dump_json()
    assert "command" not in external.options
    assert not external.deterministic
    with pytest.raises(ValueError, match="original private construction options"):
        validate_frozen_catalogue(plan.catalogue)
    validate_frozen_catalogue(plan.catalogue, {external.config_id: settings.players()[0]})
    with pytest.raises(ValueError, match="current implementation or options"):
        validate_frozen_catalogue(
            plan.catalogue,
            {external.config_id: PlayerConfig(bot="command", options={"command": ["example", "changed"]})},
        )


def test_fixed_native_plan_needs_no_registered_factory_or_private_options() -> None:
    settings = RunSettings(
        catalogue=(CandidateConfig(bot="codex"), CandidateConfig(bot="lowest_card")),
        lineup=("codex", "lowest_card"),
    )
    plan = build_arena_plan(settings)
    validate_frozen_catalogue(plan.catalogue)
    assert plan.catalogue[0].metadata["ownership"] == "attached"


def test_external_seats_reject_sampled_schedules() -> None:
    with pytest.raises(ValueError, match="external seats require an explicit fixed lineup"):
        build_arena_plan(RunSettings(catalogue=(CandidateConfig(bot="codex"),)))


@pytest.mark.parametrize("field", ["rotations", "reverse_order", "share_controlled_deals"])
@pytest.mark.parametrize("value", [0, 1, "false", "true"])
def test_run_settings_rejects_coerced_design_flags(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        RunSettings.model_validate({field: value})


@pytest.mark.parametrize("weight", [True, "1", float("inf"), float("nan")])
def test_population_settings_rejects_non_numeric_or_nonfinite_weights(weight: object) -> None:
    with pytest.raises(ValidationError):
        PopulationConfig.model_validate({
            "population_id": "custom",
            "weighting": "explicit",
            "weights": {"random": weight},
        })


@pytest.mark.parametrize("field", ["confidence_level", "practical_effect_threshold"])
@pytest.mark.parametrize("value", [True, "0.5", float("inf"), float("nan")])
def test_analysis_settings_rejects_coerced_or_nonfinite_probabilities(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        AnalysisSpec.model_validate({field: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"display": {"watch": True}, "execution": {"backend": "process"}},
        {"display": {"watch": True}, "execution": {"concurrency": 2}},
        {
            "catalogue": [{"bot": "codex"}, {"bot": "random"}],
            "lineup": ["codex", "random"],
            "execution": {"concurrency": 2},
        },
        {
            "catalogue": [{"bot": "codex-headless"}, {"bot": "random"}],
            "lineup": ["codex-headless", "random"],
            "execution": {"backend": "process"},
        },
    ],
)
def test_run_settings_rejects_unsupported_execution_capabilities(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="thread"):
        RunSettings.model_validate(changes)


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_run_settings_rejects_nonfinite_nested_catalogue_options(value: float) -> None:
    with pytest.raises(ValidationError):
        RunSettings.model_validate({"catalogue": [{"bot": "random", "options": {"nested": [{"value": value}]}}]})
