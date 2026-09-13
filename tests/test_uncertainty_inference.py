"""Check optimised inference against the original target and exact tiny models."""

import copy
import random

import emcee
import numpy as np
import pytest
from scipy.special import expit, logit

from sixnimmt.arena.bots import REGISTRY
from sixnimmt.arena.bots.base import ActionBatch, Rejection
from sixnimmt.arena.bots.simulation import ModelBasedBaitBot, SimulationBot
from sixnimmt.arena.bots.uncertainty.history import HandHistory, InferenceError, ObservedTurn, PublicHistory
from sixnimmt.arena.bots.uncertainty.inference import BatchedLegalProposal, OpponentModel, Posterior
from sixnimmt.arena.bots.uncertainty.likelihood import BatchedPosterior
from sixnimmt.arena.bots.uncertainty.options import ModelBasedBaitOptions, OpponentModelOptions, SimulationOptions
from sixnimmt.arena.runner import run_match
from sixnimmt.engine.actions import Action, SelectCardAction
from sixnimmt.engine.rules import GameRules
from sixnimmt.engine.views import MatchView, RowView

HistorySnapshots = dict[tuple[int, int], tuple[PublicHistory, MatchView]]


@pytest.fixture
def model_options() -> OpponentModelOptions:
    return OpponentModelOptions(
        policies=[
            "random",
            "highest_card",
            "lowest_card",
            "closest_gap",
            "coldest_row",
            "highest_fitting_card",
            "lowest_fitting_card",
            "hand_flexibility",
        ],
        mode="learned_mixture",
        particle_count=32,
        chain_count=4,
        draw_interval=10,
        burn_in_steps=100,
        epsilon_proposal_scale=2.5,
    )


@pytest.fixture
def posterior(model_options: OpponentModelOptions) -> Posterior:
    rows = tuple(RowView(index=i, cards=cards) for i, cards in enumerate([(1,), (30, 31, 32, 33, 34), (60,), (90,)]))
    current = HandHistory(
        rows, (), rows, turns=[ObservedTurn(rows, (("b", 50), ("c", 40))), ObservedTurn(rows, (("b", 55), ("c", 70)))]
    )
    past = (((1 / 3, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0), 3), ((1 / 2, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0), 2))
    return Posterior(current, ("b", "c"), (past, past), (2, 2), [10, 20, 25, 65, 80, 100], model_options)


def test_batched_likelihood_matches_scalar_for_every_policy_and_extreme_epsilon(posterior: Posterior) -> None:
    rng = np.random.RandomState(8)
    coordinates = np.array([
        np.r_[rng.permutation(posterior.unseen), policy, other, epsilon, -epsilon]
        for policy in range(8)
        for other in range(8)
        for epsilon in [-1000.0, -2.0, 0.0, 2.0, 1000.0]
    ])
    target = BatchedPosterior(posterior, {})
    expected = np.array([posterior(row) for row in coordinates])
    np.testing.assert_allclose(target(coordinates), expected, atol=1e-11)
    # Proposals can be rejected or reordered. Cached values must follow their
    # coordinates, never the previous proposal's acceptance outcome.
    for batch in [coordinates[::-1], coordinates[::3], coordinates]:
        np.testing.assert_allclose(target(batch), [posterior(row) for row in batch], atol=1e-11)


def test_batched_sampler_matches_enumerated_hidden_hand_and_epsilon(model_options: OpponentModelOptions) -> None:
    rows = tuple(RowView(index=i, cards=(v,)) for i, v in enumerate([1, 2, 3, 4]))
    history = HandHistory(rows, (), rows, turns=[ObservedTurn(rows, (("b", 50),))])
    options = model_options.model_copy(update={"policies": ["highest_card"], "mode": "single_policy"})
    posterior = Posterior(history, ("b",), ((),), (2,), [10, 20, 60, 70], options)
    rng = np.random.RandomState(123)
    coordinates = np.array([np.r_[rng.permutation(posterior.unseen), 0, logit(rng.uniform())] for _ in range(16)])
    sampler = emcee.EnsembleSampler(
        16,
        6,
        BatchedPosterior(posterior, {}),
        moves=emcee.moves.MHMove(BatchedLegalProposal(4, 1, options)),
        vectorize=True,
    )
    sampler.random_state = rng.get_state()
    sampler.run_mcmc(coordinates, 1600, skip_initial_state_check=True)
    samples = sampler.get_chain(discard=400, flat=True)
    assert np.mean(np.all(samples[:, :2] < 50, axis=1)) == pytest.approx(4 / 9, abs=0.035)
    assert np.mean(expit(samples[:, -1])) == pytest.approx(5 / 9, abs=0.035)


@pytest.mark.parametrize("field", ["chain_count", "draw_interval"])
def test_chain_sampling_settings_are_required(model_options: OpponentModelOptions, field: str) -> None:
    settings = model_options.model_dump()
    del settings[field]
    with pytest.raises(ValueError):
        OpponentModelOptions.model_validate(settings)


@pytest.mark.parametrize("chains", [0, 3, 64])
def test_particle_count_requires_a_positive_divisor_chain_count(
    model_options: OpponentModelOptions, chains: int
) -> None:
    with pytest.raises(ValueError):
        OpponentModelOptions.model_validate({**model_options.model_dump(), "chain_count": chains})


@pytest.mark.parametrize("mode", ["single_policy", "fixed_mixture", "learned_mixture"])
def test_batched_proposals_conserve_the_deck_and_respect_fixed_labels(
    model_options: OpponentModelOptions, mode: str
) -> None:
    options = model_options.model_copy(update={"mode": mode})
    rng = np.random.RandomState(81)
    coordinates = np.array([np.r_[rng.permutation([1, 2, 3, 4]), 0, 1, 0.0, 0.0] for _ in range(32)])
    labels = coordinates[:, 4:6].copy()
    proposal = BatchedLegalProposal(4, 2, options)
    for _ in range(50):
        coordinates, factors = proposal(coordinates, rng)
        np.testing.assert_array_equal(np.sort(coordinates[:, :4], axis=1), np.tile([1, 2, 3, 4], (32, 1)))
        np.testing.assert_array_equal(factors, np.zeros(32))
        if mode != "learned_mixture":
            np.testing.assert_array_equal(coordinates[:, 4:6], labels)


class HistoryRecorder:
    def __init__(self) -> None:
        self.history = PublicHistory()
        self.snapshots: HistorySnapshots = {}
        self.bot = REGISTRY["closest_gap"].build(1)

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action | ActionBatch:
        self.history.observe(view)
        key = (view.hand_number, view.play_number)
        if "select_card" in view.legal_actions and key not in self.snapshots:
            self.snapshots[key] = (copy.deepcopy(self.history), view)
        return self.bot.act(view, rejection)


@pytest.fixture(scope="module")
def trajectory() -> HistorySnapshots:
    recorder = HistoryRecorder()
    run_match(
        [recorder, REGISTRY["highest_card"].build(2), REGISTRY["lowest_card"].build(3)],
        123,
        rules=GameRules(target_score=10000),
        max_actions=180,
    )
    return recorder.snapshots


def test_warm_starts_remove_reveals_and_redeal_cards_without_forgetting_behaviour(
    model_options: OpponentModelOptions, trajectory: HistorySnapshots
) -> None:
    options = model_options.model_copy(update={"policies": ["highest_card", "lowest_card"], "burn_in_steps": 10})
    model = OpponentModel(options)
    for key in [(1, 1), (1, 2), (1, 5), (1, 9), (2, 1), (3, 5)]:
        history, view = trajectory[key]
        worlds = model.infer(history, view, random.Random(42))  # noqa: S311 -- reproducible sampling
        current = history.current(view)
        known = {
            *current.own_initial_hand,
            *(card for row in current.initial_rows for card in row.cards),
            *(card for turn in current.turns for _, card in turn.choices),
        }
        for world in worlds:
            sampled = [*(card for hand in world.hands for card in hand), *world.undealt]
            assert sorted(sampled) == sorted(set(range(1, 105)) - known)
            assert all(len(hand) == 11 - key[1] for hand in world.hands)
        assert model.diagnostics["observed_plays"] == (key[0] - 1) * 10 + key[1] - 1
        assert model.diagnostics["warm_start"] == (key != (1, 1))
        assert len(worlds) == options.particle_count
    assert model.diagnostics["retained_draws_per_chain"] == 8


def test_fixed_mixture_keeps_equal_chain_weight_when_retaining_multiple_draws(
    model_options: OpponentModelOptions, trajectory: HistorySnapshots
) -> None:
    options = model_options.model_copy(
        update={"policies": ["highest_card", "lowest_card"], "mode": "fixed_mixture", "burn_in_steps": 10}
    )
    model = OpponentModel(options)
    history, view = trajectory[1, 1]
    first = model.infer(history, view, random.Random(42))  # noqa: S311 -- reproducible sampling
    labels = [world.policies for world in first[: options.chain_count]]
    assert [world.policies for world in first] == labels * (options.particle_count // options.chain_count)
    for key in [(1, 2), (2, 1)]:
        history, view = trajectory[key]
        later = model.infer(history, view, random.Random(99))  # noqa: S311 -- reproducible sampling
        assert [world.policies for world in later] == [world.policies for world in first]


def test_empty_evidence_samples_prior_without_burn_in(
    model_options: OpponentModelOptions, trajectory: HistorySnapshots
) -> None:
    history, view = trajectory[1, 1]
    model = OpponentModel(model_options)
    worlds = model.infer(history, view, random.Random(42))  # noqa: S311 -- reproducible sampling
    assert len(worlds) == model_options.particle_count
    assert model.diagnostics["method"] == "exact_prior"
    assert model.diagnostics["burn_in_steps"] == 0


def test_last_card_and_bait_without_candidates_skip_inference_but_keep_history(
    model_options: OpponentModelOptions, trajectory: HistorySnapshots
) -> None:
    settings = {
        "model": model_options.model_dump(),
        "sample_count": 2,
        "horizon": 1,
        "continuation_policy": "closest_gap",
        "row_policy": {"policy": "cheapest"},
        "objective": {"kind": "mean"},
        "cutoff_evaluation": "zero",
    }
    history, view = trajectory[1, 10]
    bot = SimulationBot(1, SimulationOptions.model_validate(settings))
    bot.history = history
    action = bot.act(view)
    assert isinstance(action, SelectCardAction)
    assert action.card == view.you.hand[0]
    assert bot.model.diagnostics == {}
    assert len(bot.history.current(view).turns) == 9
    history, view = trajectory[1, 1]
    bait = ModelBasedBaitBot(
        1,
        ModelBasedBaitOptions.model_validate({**settings, "fallback_strategy": "closest_gap"}),
        REGISTRY["closest_gap"].build(1),
    )
    assert bait.act(view) == REGISTRY["closest_gap"].build(1).act(view)
    assert bait.model.diagnostics == {}
    assert bait.history.current(view).own_initial_hand == view.you.hand


def test_cached_completed_hands_match_the_full_uncached_history_target(
    model_options: OpponentModelOptions, trajectory: HistorySnapshots
) -> None:
    options = model_options.model_copy(
        update={"policies": ["highest_card", "lowest_card", "closest_gap"], "burn_in_steps": 10}
    )
    model = OpponentModel(options)
    rng = np.random.RandomState(51)
    for key in [(1, 5), (2, 5), (3, 5)]:
        history, view = trajectory[key]
        model.infer(history, view, random.Random(42))  # noqa: S311 -- reproducible sampling
        cached = model._posterior(history, view)
        fresh = OpponentModel(options)._posterior(history, view)
        coordinates = model._prior(cached, rng, 16)
        np.testing.assert_allclose(
            BatchedPosterior(cached, {})(coordinates), [fresh(row) for row in coordinates], atol=1e-11
        )


def test_warm_start_cannot_reuse_another_match_state(
    model_options: OpponentModelOptions, trajectory: HistorySnapshots
) -> None:
    model = OpponentModel(model_options)
    history, view = trajectory[1, 1]
    model.infer(history, view, random.Random(42))  # noqa: S311 -- reproducible sampling
    with pytest.raises(InferenceError, match="another match"):
        model.infer(history, view.model_copy(update={"match_id": "another"}), random.Random(42))  # noqa: S311
