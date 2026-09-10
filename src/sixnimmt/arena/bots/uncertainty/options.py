"""Explicit, bounded experiment settings for probabilistic players."""

from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from sixnimmt.arena.bots.base import BotOptions

PolicyName = Literal[
    "random",
    "lowest_card",
    "highest_card",
    "closest_gap",
    "lowest_fitting_card",
    "highest_fitting_card",
    "coldest_row",
    "hand_flexibility",
]


class OpponentModelOptions(BotOptions):
    policies: list[PolicyName] = Field(min_length=1)
    mode: Literal["single_policy", "fixed_mixture", "learned_mixture"]
    particle_count: int = Field(gt=0)
    burn_in_steps: int = Field(gt=0)
    epsilon_proposal_scale: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_catalogue(self) -> Self:
        if len(set(self.policies)) != len(self.policies):
            msg = "opponent policies must be distinct"
            raise ValueError(msg)
        if self.mode == "single_policy" and len(self.policies) != 1:
            msg = "single_policy requires exactly one policy"
            raise ValueError(msg)
        return self


class PenaltyObjective(BotOptions):
    kind: Literal["mean", "pickup_probability", "threshold_exceedance", "upper_tail"]
    threshold: int | None = Field(default=None, ge=0)
    tail_fraction: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_parameters(self) -> Self:
        if (self.kind == "threshold_exceedance") != (self.threshold is not None):
            msg = "threshold is required only for threshold_exceedance"
            raise ValueError(msg)
        if (self.kind == "upper_tail") != (self.tail_fraction is not None):
            msg = "tail_fraction is required only for upper_tail"
            raise ValueError(msg)
        return self


class RowPolicyOptions(BotOptions):
    policy: Literal["cheapest", "hand_aware"]
    max_extra_penalty: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_limit(self) -> Self:
        if (self.policy == "hand_aware") != (self.max_extra_penalty is not None):
            msg = "max_extra_penalty is required only for hand_aware row choice"
            raise ValueError(msg)
        return self


class SimulationOptions(BotOptions):
    model: OpponentModelOptions
    sample_count: int = Field(gt=0)
    horizon: int | Literal["remaining_hand"]
    continuation_policy: PolicyName
    row_policy: RowPolicyOptions
    objective: PenaltyObjective
    cutoff_evaluation: Literal["zero"]
    fallback_strategy: str = Field(min_length=1)
    fallback_options: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_horizon(self) -> Self:
        if self.horizon != "remaining_hand" and (type(self.horizon) is not int or self.horizon < 1):
            msg = "horizon must be a positive integer or remaining_hand"
            raise ValueError(msg)
        return self


class ModelBasedBaitOptions(SimulationOptions):
    @model_validator(mode="after")
    def validate_bait(self) -> Self:
        if self.horizon != 1 or self.objective.kind != "mean" or self.row_policy.policy != "cheapest":
            msg = "model-based bait requires horizon 1, mean penalty, and cheapest row choice"
            raise ValueError(msg)
        return self
