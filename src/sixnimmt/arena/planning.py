"""Deterministic expansion of declared populations and lineups into match jobs."""

import itertools
import json
import math
import random
from collections import Counter
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, JsonValue, TypeAdapter, field_serializer, field_validator, model_validator

from sixnimmt.arena.catalogue import (
    CandidateConfig,
    CatalogueEntry,
    FrozenModel,
    canonical_json,
    reference_catalogue,
    resolve_catalogue,
    stable_id,
)
from sixnimmt.arena.config import RunConfig, resolve
from sixnimmt.common.evaluation import AnalysisSpec
from sixnimmt.engine.rules import GameRules, MatchProtocol

PLAN_VERSION = 1
SEED_SCHEME_VERSION = "arena-plan-v1"

PlayerCount = Annotated[int, Field(strict=True, ge=2, le=10)]


class PopulationConfig(FrozenModel):
    population_id: str = Field(min_length=1)
    weighting: Literal["uniform", "family_balanced", "explicit"] = "uniform"
    members: tuple[str, ...] = ()
    weights: dict[str, float] = Field(default_factory=dict)


class PopulationWeight(FrozenModel):
    config_id: str
    weight: float = Field(gt=0, le=1, allow_inf_nan=False)


class CompositionWeight(FrozenModel):
    player_count: PlayerCount
    opponents: tuple[str, ...]
    weight: float = Field(gt=0, le=1, allow_inf_nan=False)


class Population(FrozenModel):
    population_id: str
    population_version: str = ""
    kind: Literal["iid", "subsets"] = "iid"
    weights: tuple[PopulationWeight, ...] = ()
    composition_weights: tuple[CompositionWeight, ...] = ()

    @model_validator(mode="after")
    def check_weights(self) -> "Population":
        if self.kind == "iid":
            if len(self.weights) == 0 or len(self.composition_weights) != 0:
                msg = "IID populations require entry weights only"
                raise ValueError(msg)
            if len({item.config_id for item in self.weights}) != len(self.weights):
                msg = "population entry weights must be unique"
                raise ValueError(msg)
            totals = (sum(item.weight for item in self.weights),)
        else:
            if len(self.composition_weights) == 0 or len(self.weights) != 0:
                msg = "subset populations require composition weights only"
                raise ValueError(msg)
            counts = {item.player_count for item in self.composition_weights}
            totals = tuple(
                sum(item.weight for item in self.composition_weights if item.player_count == n) for n in counts
            )
        if any(not math.isclose(total, 1.0, rel_tol=1e-9) for total in totals):
            msg = "population weights must sum to one within each player count"
            raise ValueError(msg)
        version = stable_id(
            "population",
            [
                self.kind,
                [item.model_dump() for item in self.weights],
                [item.model_dump() for item in self.composition_weights],
            ],
        )
        if self.population_version != "" and self.population_version != version:
            msg = "population version does not match its frozen weights"
            raise ValueError(msg)
        object.__setattr__(self, "population_version", version)
        return self


class ReplacementComparison(FrozenModel):
    """Replace one focal instance while preserving backgrounds, slots and seeds."""

    comparison_id: str = Field(min_length=1)
    reference: str
    candidates: tuple[str, ...] = Field(min_length=1)
    backgrounds: tuple[tuple[str, ...], ...] = ()
    group_roster: tuple[str, ...] = ()
    background_population: str | None = None
    background_draws: int = Field(default=0, ge=0)
    games: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def check_background(self) -> "ReplacementComparison":
        sources = int(len(self.backgrounds) > 0) + int(len(self.group_roster) > 0)
        sources += int(self.background_population is not None)
        if sources != 1:
            msg = "a replacement comparison needs exactly one of backgrounds, group_roster, or background_population"
            raise ValueError(msg)
        if self.background_population is not None and self.background_draws < 1:
            msg = "sampled comparison backgrounds require positive background_draws"
            raise ValueError(msg)
        if self.background_population is None and self.background_draws != 0:
            msg = "background_draws only applies to a background_population"
            raise ValueError(msg)
        return self


class ResolvedComparison(FrozenModel):
    comparison_id: str
    reference: str
    candidates: tuple[str, ...]
    population_id: str


class LineupConfig(FrozenModel):
    catalogue: tuple[CandidateConfig, ...] = Field(default_factory=reference_catalogue)
    catalogue_version: str = "reference-v1"
    player_counts: tuple[PlayerCount, ...] = (4,)
    games: int = Field(default=100, ge=0)
    controlled_games: int = Field(default=0, ge=0)
    seed: int = 66
    population_id: str = "uniform"
    populations: tuple[PopulationConfig, ...] = ()
    controlled_coverage: Literal["pairs", "exhaustive", "explicit"] = "pairs"
    compositions: tuple[tuple[str, ...], ...] = ()
    comparisons: tuple[ReplacementComparison, ...] = ()
    rotations: bool = True
    reverse_order: bool = False
    additional_permutations: int = Field(default=0, ge=0)
    share_controlled_deals: bool = False
    rules: GameRules = Field(default_factory=GameRules)
    protocol: MatchProtocol = Field(default_factory=lambda: MatchProtocol(anonymise_display_names=True))
    execution: RunConfig = Field(default_factory=RunConfig)
    analysis: AnalysisSpec = Field(default_factory=AnalysisSpec)

    @field_validator("protocol", mode="before")
    @classmethod
    def anonymous_protocol(cls, value: object) -> object:
        if isinstance(value, MatchProtocol):
            return value.model_copy(update={"anonymise_display_names": True})
        if isinstance(value, dict):
            return {**value, "anonymise_display_names": True}
        return value

    @model_validator(mode="after")
    def check_design(self) -> "LineupConfig":
        if len(self.player_counts) == 0 or len(set(self.player_counts)) != len(self.player_counts):
            msg = "player_counts must contain distinct supported counts"
            raise ValueError(msg)
        if not self.protocol.anonymise_display_names:
            msg = "planned comparisons require anonymise_display_names=true"
            raise ValueError(msg)
        for count in self.player_counts:
            if not self.rules.min_players <= count <= self.rules.max_players:
                msg = "player_counts fall outside the game rules"
                raise ValueError(msg)
        if self.games == 0 and self.controlled_games == 0 and len(self.comparisons) == 0:
            msg = "lineup settings must request at least one game"
            raise ValueError(msg)
        return self


class SeatAssignment(FrozenModel):
    seat: int = Field(ge=0)
    config_id: str
    instance_id: str
    bot_seed: int


class PlannedMatch(FrozenModel):
    job_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    match_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    player_count: PlayerCount
    seats: tuple[SeatAssignment, ...]
    match_seed: int
    condition_id: str
    stream: Literal["iid", "controlled", "matched", "fixed"]
    block_id: str
    shared_deal_id: str
    lineup_draw_id: str | None = None
    population_id: str | None = None
    comparison_id: str | None = None
    arm_id: str | None = None
    focal_seat: int | None = None
    rotation: int = Field(default=0, ge=0)
    permutation: int = Field(default=0, ge=0)
    selection_probability: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    planned_quota: int | None = Field(default=None, ge=1)

    @property
    def lineup(self) -> tuple[str, ...]:
        return tuple(seat.config_id for seat in self.seats)

    def opponent_composition(self, seat: int) -> tuple[str, ...]:
        return tuple(sorted(assignment.config_id for assignment in self.seats if assignment.seat != seat))

    @model_validator(mode="after")
    def check_seats(self) -> "PlannedMatch":
        if len(self.seats) != self.player_count or tuple(seat.seat for seat in self.seats) != tuple(
            range(self.player_count)
        ):
            msg = "planned seats must enumerate every player exactly once in order"
            raise ValueError(msg)
        if len({seat.instance_id for seat in self.seats}) != self.player_count:
            msg = "each planned seat requires a distinct instance identity"
            raise ValueError(msg)
        if self.focal_seat is not None and not 0 <= self.focal_seat < self.player_count:
            msg = "focal_seat is outside the planned seats"
            raise ValueError(msg)
        return self


class ArenaPlan(FrozenModel):
    version: Literal[1] = PLAN_VERSION
    plan_id: str
    catalogue_version: str
    seed_scheme_version: Literal["arena-plan-v1"] = SEED_SCHEME_VERSION
    seed: int
    catalogue: tuple[CatalogueEntry, ...]
    player_counts: tuple[PlayerCount, ...]
    populations: tuple[Population, ...]
    comparisons: tuple[ResolvedComparison, ...]
    jobs: tuple[PlannedMatch, ...]
    rules: GameRules
    protocol: MatchProtocol
    execution: RunConfig
    analysis: AnalysisSpec
    design_json: str = Field(alias="design", repr=False)

    @field_validator("design_json", mode="before")
    @classmethod
    def freeze_design(cls, value: object) -> str:
        return canonical_json(json.loads(value) if isinstance(value, str) else value)

    @field_serializer("design_json")
    def serialize_design(self, value: str) -> dict[str, JsonValue]:
        return json.loads(value)

    @property
    def design(self) -> dict[str, JsonValue]:
        return json.loads(self.design_json)

    @model_validator(mode="after")
    def check_contract(self) -> "ArenaPlan":
        _validate_plan_contract(self)
        return self


@dataclass(frozen=True)
class _Block:
    lineup: tuple[str, ...]
    condition_id: str
    stream: Literal["iid", "controlled", "matched"]
    block_id: str
    shared_deal_id: str
    game_index: int = 0
    lineup_draw_id: str | None = None
    population_id: str | None = None
    comparison_id: str | None = None
    arm_id: str | None = None
    selection_probability: float | None = None
    planned_quota: int | None = None


def _seed(root: int, *parts: object) -> int:
    return int(stable_id("seed", [SEED_SCHEME_VERSION, root, *parts]).split("-", 1)[1], 16) % (2**63)


def _resolve_key(key: str, catalogue: tuple[CatalogueEntry, ...]) -> str:
    for entry in catalogue:
        if key == entry.key or key == entry.config_id:
            return entry.config_id
    msg = f"unknown catalogue key: {key}"
    raise ValueError(msg)


def _resolve_lineup(keys: tuple[str, ...], catalogue: tuple[CatalogueEntry, ...]) -> tuple[str, ...]:
    return tuple(sorted(_resolve_key(key, catalogue) for key in keys))


def _population(config: PopulationConfig, catalogue: tuple[CatalogueEntry, ...]) -> Population:
    if len(config.members) == 0:
        members = catalogue
    else:
        selected = {_resolve_key(key, catalogue) for key in config.members}
        if len(selected) != len(config.members):
            msg = "population members must be distinct"
            raise ValueError(msg)
        members = tuple(entry for entry in catalogue if entry.config_id in selected)
    raw_weights = _population_weights(config, members)
    total = sum(raw_weights.values())
    if total <= 0 or not math.isfinite(total):
        msg = "population weights must have a finite positive total"
        raise ValueError(msg)
    return Population(
        population_id=config.population_id,
        weights=tuple(PopulationWeight(config_id=key, weight=value / total) for key, value in raw_weights.items()),
    )


def _population_weights(config: PopulationConfig, members: tuple[CatalogueEntry, ...]) -> dict[str, float]:
    if config.weighting == "explicit":
        weights = {_resolve_key(key, members): value for key, value in config.weights.items()}
        if len(weights) != len(config.weights) or len(weights) == 0:
            msg = "explicit population weights need distinct entries"
            raise ValueError(msg)
        if any(not math.isfinite(value) or value <= 0 for value in weights.values()):
            msg = "explicit population weights must be finite and positive"
            raise ValueError(msg)
        return weights
    if len(config.weights) > 0:
        msg = "weights require weighting='explicit'"
        raise ValueError(msg)
    if config.weighting == "family_balanced":
        families = Counter(entry.family for entry in members)
        return {entry.config_id: 1 / families[entry.family] for entry in members}
    return {entry.config_id: 1.0 for entry in members}


def _populations(config: LineupConfig, catalogue: tuple[CatalogueEntry, ...]) -> list[Population]:
    definitions = [
        PopulationConfig(population_id="uniform"),
        PopulationConfig(population_id="family_balanced", weighting="family_balanced"),
    ]
    custom_names = [population.population_id for population in config.populations]
    if len(set(custom_names)) != len(custom_names):
        msg = "population IDs must be unique"
        raise ValueError(msg)
    definitions = [population for population in definitions if population.population_id not in custom_names]
    definitions.extend(config.populations)
    return [_population(population, catalogue) for population in definitions]


def _get_population(name: str, populations: list[Population]) -> Population:
    for population in populations:
        if population.population_id == name:
            return population
    msg = f"unknown population: {name}"
    raise ValueError(msg)


def _draw(population: Population, count: int, seed: int) -> tuple[tuple[str, ...], float]:
    entries = tuple(item.config_id for item in population.weights)
    weights = tuple(item.weight for item in population.weights)
    if len(entries) == 0:
        msg = "IID sampling requires an entry-weighted population"
        raise ValueError(msg)
    rng = random.Random(seed)  # noqa: S311 - reproducible schedule sampling
    lineup = tuple(rng.choices(entries, weights=weights, k=count))
    probabilities = dict(zip(entries, weights, strict=True))
    return lineup, math.prod(probabilities[key] for key in lineup)


def _random_games(config: LineupConfig, count: int, population: Population) -> list[_Block]:
    blocks: list[_Block] = []
    for draw in range(config.games):
        draw_id = stable_id("draw", ["iid", count, population.population_id, draw])
        lineup, probability = _draw(population, count, _seed(config.seed, draw_id, "lineup"))
        blocks.append(
            _Block(
                lineup=lineup,
                condition_id=stable_id("composition", sorted(lineup)),
                stream="iid",
                block_id=draw_id,
                shared_deal_id=stable_id("deal", draw_id),
                game_index=draw,
                lineup_draw_id=draw_id,
                population_id=population.population_id,
                selection_probability=probability,
                planned_quota=config.games,
            )
        )
    return blocks


def _controlled_compositions(
    config: LineupConfig, count: int, catalogue: tuple[CatalogueEntry, ...]
) -> list[tuple[str, ...]]:
    entries = tuple(entry.config_id for entry in catalogue)
    compositions = set()
    if config.controlled_coverage == "exhaustive":
        compositions.update(itertools.combinations_with_replacement(sorted(entries), count))
    elif config.controlled_coverage == "pairs":
        compositions.update((entry,) * count for entry in entries)
        for first, second in itertools.combinations(entries, 2):
            compositions.update(
                tuple(sorted((first,) * copies + (second,) * (count - copies))) for copies in range(1, count)
            )
    for explicit in config.compositions:
        if len(explicit) == count:
            compositions.add(_resolve_lineup(explicit, catalogue))
    if len(compositions) == 0:
        msg = f"no controlled compositions selected for {count} players"
        raise ValueError(msg)
    return sorted(compositions)


def _controlled_games(config: LineupConfig, count: int, catalogue: tuple[CatalogueEntry, ...]) -> list[_Block]:
    if config.controlled_games == 0:
        return []
    blocks = []
    for lineup in _controlled_compositions(config, count, catalogue):
        condition_id = stable_id("composition", lineup)
        for deal in range(config.controlled_games):
            scope = "controlled" if config.share_controlled_deals else condition_id
            deal_id = stable_id("deal", [count, scope, deal])
            blocks.append(
                _Block(
                    lineup=lineup,
                    condition_id=condition_id,
                    stream="controlled",
                    block_id=deal_id,
                    shared_deal_id=deal_id,
                    game_index=deal,
                    planned_quota=config.controlled_games,
                )
            )
    return blocks


def _fixed_backgrounds(
    comparison: ReplacementComparison, count: int, catalogue: tuple[CatalogueEntry, ...]
) -> list[tuple[str, ...]]:
    if len(comparison.group_roster) > 0:
        roster = _resolve_lineup(comparison.group_roster, catalogue)
        if len(set(roster)) != len(roster):
            msg = "group_roster must have distinct configurations"
            raise ValueError(msg)
        backgrounds = list(itertools.combinations(roster, count - 1))
    else:
        backgrounds = sorted({
            _resolve_lineup(background, catalogue)
            for background in comparison.backgrounds
            if len(background) == count - 1
        })
    if len(backgrounds) == 0:
        msg = f"comparison {comparison.comparison_id} has no backgrounds for {count} players"
        raise ValueError(msg)
    return backgrounds


def _comparison_population(
    comparison: ReplacementComparison,
    config: LineupConfig,
    catalogue: tuple[CatalogueEntry, ...],
    populations: list[Population],
) -> Population:
    if comparison.background_population is not None:
        return _get_population(comparison.background_population, populations)
    cells = []
    for count in config.player_counts:
        backgrounds = _fixed_backgrounds(comparison, count, catalogue)
        cells.extend(
            CompositionWeight(player_count=count, opponents=background, weight=1 / len(backgrounds))
            for background in backgrounds
        )
    name = f"backgrounds:{comparison.comparison_id}"
    if any(population.population_id == name for population in populations):
        msg = f"duplicate population ID: {name}"
        raise ValueError(msg)
    population = Population(population_id=name, kind="subsets", composition_weights=tuple(cells))
    populations.append(population)
    return population


def _comparison_backgrounds(
    config: LineupConfig, comparison: ReplacementComparison, count: int, population: Population
) -> list[tuple[tuple[str, ...], str | None, float | None]]:
    if population.kind == "subsets":
        return [(cell.opponents, None, None) for cell in population.composition_weights if cell.player_count == count]
    backgrounds = []
    for draw in range(comparison.background_draws):
        draw_id = stable_id("draw", ["matched", comparison.comparison_id, count, draw])
        background, probability = _draw(population, count - 1, _seed(config.seed, draw_id, "lineup"))
        backgrounds.append((background, draw_id, probability))
    return backgrounds


def _matched_blocks(
    config: LineupConfig,
    comparison: ReplacementComparison,
    resolved: ResolvedComparison,
    count: int,
    population: Population,
) -> list[_Block]:
    blocks = []
    for background, draw_id, probability in _comparison_backgrounds(config, comparison, count, population):
        condition_id = stable_id("background", [count, sorted(background)])
        for deal in range(comparison.games):
            source = condition_id if draw_id is None else draw_id
            scope = "matched" if config.share_controlled_deals and draw_id is None else source
            deal_id = stable_id("deal", [count, comparison.comparison_id, scope, deal])
            for arm in (resolved.reference, *resolved.candidates):
                blocks.append(
                    _Block(
                        lineup=(arm, *background),
                        condition_id=condition_id,
                        stream="matched",
                        block_id=deal_id if draw_id is None else draw_id,
                        shared_deal_id=deal_id,
                        game_index=deal,
                        lineup_draw_id=draw_id,
                        population_id=population.population_id,
                        comparison_id=comparison.comparison_id,
                        arm_id=arm,
                        selection_probability=probability,
                        planned_quota=comparison.games,
                    )
                )
    return blocks


def _permutation_from_rank(values: list[int], rank: int) -> tuple[int, ...]:
    remaining = list(values)
    result = []
    while len(remaining) > 0:
        divisor = math.factorial(len(remaining) - 1)
        index, rank = divmod(rank, divisor)
        result.append(remaining.pop(index))
    return tuple(result)


def _order_key(block: _Block) -> tuple[str, str, str | None, str | None]:
    return block.stream, block.condition_id, block.comparison_id, block.lineup_draw_id


def _seat_orders(config: LineupConfig, block: _Block) -> list[tuple[int, ...]]:
    count = len(block.lineup)
    indices = list(range(count))
    rng = random.Random(_seed(config.seed, *_order_key(block), "order"))  # noqa: S311
    movable = indices[1:] if block.stream == "matched" else indices
    rng.shuffle(movable)
    prefix = (0,) if block.stream == "matched" else ()
    orders = [prefix + tuple(movable)]
    available = math.factorial(len(movable))
    reverse = config.reverse_order and available > 1
    if reverse:
        orders.append(prefix + tuple(reversed(movable)))
    ranks = range(1, available - 1 if reverse else available)
    quota = 1 if block.planned_quota is None else block.planned_quota
    rotations = count if config.rotations else 1
    usable_orders = (quota + rotations - 1) // rotations
    requested = min(config.additional_permutations, len(ranks), max(0, usable_orders - len(orders)))
    for rank in rng.sample(ranks, k=requested):
        orders.append(prefix + _permutation_from_rank(movable, rank))
    return orders


def _job(config: LineupConfig, block: _Block, order: tuple[int, ...], permutation: int, rotation: int) -> PlannedMatch:
    rotated = order[rotation:] + order[:rotation]
    identity = [block.stream, block.shared_deal_id, block.condition_id, block.arm_id, permutation, rotation]
    job_id = stable_id("job", identity)
    seats = tuple(
        SeatAssignment(
            seat=seat,
            config_id=block.lineup[instance],
            instance_id=stable_id("instance", [job_id, instance]),
            bot_seed=_seed(config.seed, block.shared_deal_id, block.condition_id, "bot", instance),
        )
        for seat, instance in enumerate(rotated)
    )
    return PlannedMatch(
        job_id=job_id,
        match_id=stable_id("match", [config.seed, job_id]),
        player_count=len(seats),
        seats=seats,
        match_seed=_seed(config.seed, block.shared_deal_id, "match"),
        condition_id=block.condition_id,
        stream=block.stream,
        block_id=block.block_id,
        shared_deal_id=block.shared_deal_id,
        lineup_draw_id=block.lineup_draw_id,
        population_id=block.population_id,
        comparison_id=block.comparison_id,
        arm_id=block.arm_id,
        focal_seat=rotated.index(0) if block.stream == "matched" else None,
        rotation=rotation,
        permutation=permutation,
        selection_probability=block.selection_probability,
        planned_quota=block.planned_quota,
    )


def _planned_jobs(config: LineupConfig, blocks: list[_Block]) -> list[PlannedMatch]:
    jobs = []
    orders_by_condition: dict[tuple[str, str, str | None, str | None], list[tuple[int, ...]]] = {}
    for block in blocks:
        count = len(block.lineup)
        if block.stream == "iid":
            jobs.append(_job(config, block, tuple(range(count)), 0, 0))
            continue
        key = _order_key(block)
        if key not in orders_by_condition:
            orders_by_condition[key] = _seat_orders(config, block)
        orders = orders_by_condition[key]
        rotation_count = count if config.rotations else 1
        rotation = block.game_index % rotation_count
        permutation = (block.game_index // rotation_count) % len(orders)
        jobs.append(_job(config, block, orders[permutation], permutation, rotation))
    return jobs


def _validate_references(config: LineupConfig, catalogue: tuple[CatalogueEntry, ...]) -> None:
    for composition in config.compositions:
        if len(composition) not in config.player_counts:
            msg = "explicit composition length must match a selected player count"
            raise ValueError(msg)
        _resolve_lineup(composition, catalogue)
    for comparison in config.comparisons:
        for background in comparison.backgrounds:
            if len(background) + 1 not in config.player_counts:
                msg = "comparison background length must match a selected player count minus one"
                raise ValueError(msg)
            _resolve_lineup(background, catalogue)


def _resolve_comparisons(
    config: LineupConfig, catalogue: tuple[CatalogueEntry, ...], populations: list[Population]
) -> tuple[ResolvedComparison, ...]:
    resolved = []
    seen: set[str] = set()
    for comparison in config.comparisons:
        if comparison.comparison_id in seen:
            msg = "comparison IDs must be unique"
            raise ValueError(msg)
        seen.add(comparison.comparison_id)
        reference = _resolve_key(comparison.reference, catalogue)
        candidates = tuple(_resolve_key(key, catalogue) for key in comparison.candidates)
        if reference in candidates or len(set(candidates)) != len(candidates):
            msg = "comparison candidates must be distinct from each other and the reference"
            raise ValueError(msg)
        population = _comparison_population(comparison, config, catalogue, populations)
        resolved.append(
            ResolvedComparison(
                comparison_id=comparison.comparison_id,
                reference=reference,
                candidates=candidates,
                population_id=population.population_id,
            )
        )
    return tuple(resolved)


def build_arena_plan(config: LineupConfig) -> ArenaPlan:
    """Resolve the complete schedule before any bot is constructed or outcome observed."""
    catalogue = resolve_catalogue(config.catalogue)
    _validate_references(config, catalogue)
    populations = _populations(config, catalogue)
    selected_population = _get_population(config.population_id, populations)
    comparisons = _resolve_comparisons(config, catalogue, populations)
    blocks = []
    for count in config.player_counts:
        blocks.extend(_random_games(config, count, selected_population))
        blocks.extend(_controlled_games(config, count, catalogue))
        for definition, comparison in zip(config.comparisons, comparisons, strict=True):
            population = _get_population(comparison.population_id, populations)
            blocks.extend(_matched_blocks(config, definition, comparison, count, population))
    jobs = _planned_jobs(config, blocks)
    rng = random.Random(_seed(config.seed, "execution_order"))  # noqa: S311
    rng.shuffle(jobs)
    execution = resolve(config.execution, config.protocol)
    design = config.model_dump(mode="json")
    design["execution"] = TypeAdapter(RunConfig).dump_python(execution, mode="json")
    return ArenaPlan(
        plan_id=stable_id(
            "plan", {"design": design, "catalogue": [entry.model_dump(mode="json") for entry in catalogue]}
        ),
        catalogue_version=config.catalogue_version,
        seed=config.seed,
        catalogue=catalogue,
        player_counts=config.player_counts,
        populations=tuple(populations),
        comparisons=comparisons,
        jobs=tuple(jobs),
        rules=config.rules,
        protocol=config.protocol,
        execution=execution,
        analysis=config.analysis,
        design_json=canonical_json(design),
    )


def with_execution(plan: ArenaPlan, config: RunConfig) -> ArenaPlan:
    """Freeze changed execution limits without changing assignments or seed streams."""
    execution = resolve(config, plan.protocol)
    recorded_execution = TypeAdapter(RunConfig).dump_python(execution, mode="json")
    design = plan.design
    design["execution"] = recorded_execution
    data = plan.model_dump(mode="json")
    data["execution"] = recorded_execution
    data["design"] = design
    data["plan_id"] = stable_id(
        "plan", {"design": design, "catalogue": [entry.model_dump(mode="json") for entry in plan.catalogue]}
    )
    return ArenaPlan.model_validate(data)


def _require_distinct(values: tuple[str, ...], description: str) -> None:
    if len(set(values)) != len(values):
        msg = f"{description} must be unique"
        raise ValueError(msg)


def _validate_plan_populations(plan: ArenaPlan, configurations: set[str]) -> None:
    for population in plan.populations:
        members = {item.config_id for item in population.weights}
        for cell in population.composition_weights:
            if cell.player_count not in plan.player_counts or len(cell.opponents) != cell.player_count - 1:
                msg = "population composition has an invalid player count"
                raise ValueError(msg)
            members.update(cell.opponents)
        if not members <= configurations:
            msg = "population references an unknown configuration"
            raise ValueError(msg)


def _validate_plan_jobs(plan: ArenaPlan, configurations: set[str], populations: set[str]) -> None:
    comparisons = {comparison.comparison_id: comparison for comparison in plan.comparisons}
    for job in plan.jobs:
        if job.player_count not in plan.player_counts:
            msg = "job player count is absent from the plan"
            raise ValueError(msg)
        if not set(job.lineup) <= configurations:
            msg = "job references an unknown configuration"
            raise ValueError(msg)
        if job.population_id is not None and job.population_id not in populations:
            msg = "job references an unknown population"
            raise ValueError(msg)
        if job.stream == "matched":
            _validate_matched_job(job, comparisons)
        elif job.comparison_id is not None or job.arm_id is not None or job.focal_seat is not None:
            msg = "comparison metadata requires a matched job"
            raise ValueError(msg)


def _validate_matched_job(job: PlannedMatch, comparisons: dict[str, ResolvedComparison]) -> None:
    if job.comparison_id not in comparisons:
        msg = "matched job references an unknown comparison"
        raise ValueError(msg)
    comparison = comparisons[job.comparison_id]
    if job.arm_id not in (comparison.reference, *comparison.candidates):
        msg = "matched job references an unknown arm"
        raise ValueError(msg)
    if job.focal_seat is None or job.seats[job.focal_seat].config_id != job.arm_id:
        msg = "matched arm must identify the focal configuration"
        raise ValueError(msg)
    if job.population_id != comparison.population_id:
        msg = "matched job population differs from its comparison"
        raise ValueError(msg)


def _validate_plan_contract(plan: ArenaPlan) -> None:
    _require_distinct(tuple(entry.config_id for entry in plan.catalogue), "configuration IDs")
    _require_distinct(tuple(entry.key for entry in plan.catalogue), "catalogue keys")
    _require_distinct(tuple(population.population_id for population in plan.populations), "population IDs")
    _require_distinct(tuple(comparison.comparison_id for comparison in plan.comparisons), "comparison IDs")
    _require_distinct(tuple(job.job_id for job in plan.jobs), "job IDs")
    _require_distinct(tuple(job.match_id for job in plan.jobs), "match IDs")
    configurations = {entry.config_id for entry in plan.catalogue}
    populations = {population.population_id for population in plan.populations}
    if len(plan.player_counts) == 0 or len(set(plan.player_counts)) != len(plan.player_counts):
        msg = "plan player counts must be distinct and nonempty"
        raise ValueError(msg)
    if any(not plan.rules.min_players <= count <= plan.rules.max_players for count in plan.player_counts):
        msg = "plan player counts fall outside the game rules"
        raise ValueError(msg)
    if not plan.protocol.anonymise_display_names:
        msg = "planned matches require anonymous display names"
        raise ValueError(msg)
    for comparison in plan.comparisons:
        if (
            not {comparison.reference, *comparison.candidates} <= configurations
            or comparison.population_id not in populations
        ):
            msg = "comparison references an unknown configuration or population"
            raise ValueError(msg)
    _validate_plan_populations(plan, configurations)
    _validate_plan_jobs(plan, configurations, populations)
