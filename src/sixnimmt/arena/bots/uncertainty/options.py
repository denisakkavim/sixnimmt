"""Explicit, bounded experiment settings for probabilistic players."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, JsonValue, TypeAdapter, field_validator, model_validator

from sixnimmt.arena.bots.base import BotOptions

DeterministicPolicyName = Literal[
    "lowest_card",
    "highest_card",
    "closest_gap",
    "lowest_fitting_card",
    "highest_fitting_card",
    "coldest_row",
    "hand_flexibility",
]
PolicyName = Literal["random", DeterministicPolicyName]


class OpponentModelOptions(BotOptions):
    policies: tuple[PolicyName, ...] = Field(min_length=1)
    mode: Literal["single_policy", "fixed_mixture", "learned_mixture"]
    particle_count: int = Field(gt=0)
    chain_count: int = Field(gt=0)
    draw_interval: int = Field(gt=0)
    burn_in_steps: int = Field(gt=0)
    epsilon_proposal_scale: float = Field(gt=0, allow_inf_nan=False)

    @field_validator("policies", mode="before")
    @classmethod
    def normalize_policies(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_catalogue(self) -> Self:
        if self.particle_count % self.chain_count != 0:
            msg = "particle_count must be a multiple of chain_count"
            raise ValueError(msg)
        if len(set(self.policies)) != len(self.policies):
            msg = "opponent policies must be distinct"
            raise ValueError(msg)
        if self.mode == "single_policy" and len(self.policies) != 1:
            msg = "single_policy requires exactly one policy"
            raise ValueError(msg)
        return self


class MeanPenalty(BotOptions):
    kind: Literal["mean"] = "mean"


class PickupProbability(BotOptions):
    kind: Literal["pickup_probability"] = "pickup_probability"


class ThresholdExceedance(BotOptions):
    kind: Literal["threshold_exceedance"] = "threshold_exceedance"
    threshold: int = Field(ge=0)


class UpperTailPenalty(BotOptions):
    kind: Literal["upper_tail"] = "upper_tail"
    tail_fraction: float = Field(gt=0, le=1, allow_inf_nan=False)


def _normalize_objective(value: object) -> object:
    # Older serialized options included irrelevant null placeholders. Continue
    # accepting only those placeholders; non-null unsupported fields still fail.
    if not isinstance(value, dict):
        return value
    fields = {"threshold_exceedance": "threshold", "upper_tail": "tail_fraction"}
    relevant = fields.get(value.get("kind"))
    return {
        key: item
        for key, item in value.items()
        if key == relevant or key not in ("threshold", "tail_fraction") or item is not None
    }


PenaltyObjective = Annotated[
    MeanPenalty | PickupProbability | ThresholdExceedance | UpperTailPenalty,
    Field(discriminator="kind"),
    BeforeValidator(_normalize_objective),
]
_OBJECTIVE_ADAPTER: TypeAdapter[PenaltyObjective] = TypeAdapter(PenaltyObjective)


def parse_penalty_objective(value: object) -> PenaltyObjective:
    return _OBJECTIVE_ADAPTER.validate_python(value)


class CheapestRow(BotOptions):
    policy: Literal["cheapest"] = "cheapest"


class HandAwareRow(BotOptions):
    policy: Literal["hand_aware"] = "hand_aware"
    max_extra_penalty: int = Field(ge=0)


def _normalize_row_policy(value: object) -> object:
    if isinstance(value, dict) and value.get("policy") == "cheapest" and value.get("max_extra_penalty") is None:
        return {key: item for key, item in value.items() if key != "max_extra_penalty"}
    return value


RowPolicyOptions = Annotated[
    CheapestRow | HandAwareRow, Field(discriminator="policy"), BeforeValidator(_normalize_row_policy)
]
_ROW_POLICY_ADAPTER: TypeAdapter[RowPolicyOptions] = TypeAdapter(RowPolicyOptions)


def parse_row_policy(value: object) -> RowPolicyOptions:
    return _ROW_POLICY_ADAPTER.validate_python(value)


Horizon = Annotated[int, Field(strict=True, gt=0)] | Literal["remaining_hand"]


class EvaluationOptions(BotOptions):
    model: OpponentModelOptions
    sample_count: int = Field(gt=0)
    continuation_policy: PolicyName
    cutoff_evaluation: Literal["zero"]


class SimulationOptions(EvaluationOptions):
    horizon: Horizon
    row_policy: RowPolicyOptions
    objective: PenaltyObjective


class ModelBasedBaitOptions(EvaluationOptions):
    horizon: Literal[1]
    row_policy: Annotated[CheapestRow, BeforeValidator(_normalize_row_policy)]
    objective: Annotated[MeanPenalty, BeforeValidator(_normalize_objective)]
    fallback_strategy: str = Field(min_length=1)
    fallback_options: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("horizon", mode="before")
    @classmethod
    def check_horizon_type(cls, value: object) -> object:
        if type(value) is not int:
            msg = "horizon must be the integer 1"
            raise ValueError(msg)
        return value
