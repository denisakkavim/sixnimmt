"""Posterior correctness, hidden information, and engine-backed lookahead."""

import math
import random
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

import emcee
import numpy as np
import pytest
from pydantic import ValidationError
from scipy.special import expit, logit

from sixnimmt.arena.bots import REGISTRY
from sixnimmt.arena.bots.base import ActionBatch
from sixnimmt.arena.bots.simulation import SimulationBot
from sixnimmt.arena.bots.uncertainty.history import HandHistory, ObservedTurn, PublicHistory
from sixnimmt.arena.bots.uncertainty.inference import LegalProposal, OpponentModel, Posterior, World, log_likelihood
from sixnimmt.arena.bots.uncertainty.options import (
    ModelBasedBaitOptions,
    PenaltyObjective,
    PolicyName,
    RowPolicyOptions,
    SimulationOptions,
)
from sixnimmt.arena.bots.uncertainty.policies import probability
from sixnimmt.arena.bots.uncertainty.simulation import initial_state, penalty_value, resolve_turn, rollout
from sixnimmt.arena.config import RunConfig
from sixnimmt.arena.players import PlayerConfig, resolve_players
from sixnimmt.arena.runner import run_arena, run_match
from sixnimmt.engine.actions import ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.audience import Viewer
from sixnimmt.engine.fold import build_view
from sixnimmt.engine.rules import GameRules, MatchProtocol
from sixnimmt.engine.setup import create_match
from sixnimmt.engine.state import Phase
from sixnimmt.engine.views import MatchView, OpponentView, PlayerSelfView, RowView, ViewRole


@pytest.fixture
def options() -> SimulationOptions:
    return SimulationOptions.model_validate({
        "model": {
            "policies": ["highest_card", "lowest_card"],
            "mode": "learned_mixture",
            "particle_count": 4,
            "burn_in_steps": 8,
            "epsilon_proposal_scale": 1.0,
        },
        "sample_count": 3,
        "horizon": 1,
        "continuation_policy": "closest_gap",
        "row_policy": {"policy": "cheapest"},
        "objective": {"kind": "mean"},
        "cutoff_evaluation": "zero",
        "fallback_strategy": "closest_gap",
    })


@pytest.fixture
def view() -> MatchView:
    _, events = create_match("uncertainty", ["a", "b", "c"], 123)
    return build_view(events, Viewer(ViewRole.PLAYER, "a"))


@pytest.mark.parametrize(
    "field",
    [
        "model",
        "sample_count",
        "horizon",
        "continuation_policy",
        "row_policy",
        "objective",
        "cutoff_evaluation",
        "fallback_strategy",
    ],
)
def test_requires_explicit_experiment_settings(options: SimulationOptions, field: str) -> None:
    settings = options.model_dump()
    del settings[field]
    with pytest.raises(ValidationError):
        SimulationOptions.model_validate(settings)


@pytest.mark.parametrize("horizon", [0, -1, True, 1.5, "2"])
def test_rejects_invalid_horizon(options: SimulationOptions, horizon: object) -> None:
    with pytest.raises(ValidationError):
        SimulationOptions.model_validate({**options.model_dump(), "horizon": horizon})


@pytest.mark.parametrize(
    "settings",
    [
        {"kind": "threshold_exceedance"},
        {"kind": "upper_tail"},
        {"kind": "upper_tail", "tail_fraction": 0},
        {"kind": "mean", "threshold": 2},
    ],
)
def test_requires_only_relevant_objective_parameters(settings: dict) -> None:
    with pytest.raises(ValidationError):
        PenaltyObjective.model_validate(settings)


@pytest.mark.parametrize(
    ("objective", "expected"),
    [
        ({"kind": "mean"}, 3.5),
        ({"kind": "pickup_probability"}, 0.75),
        ({"kind": "threshold_exceedance", "threshold": 4}, 0.25),
        ({"kind": "upper_tail", "tail_fraction": 0.375}, 20 / 3),
        ({"kind": "upper_tail", "tail_fraction": 1.0}, 3.5),
    ],
)
def test_penalty_objectives_include_fractional_tail_mass(objective: dict, expected: float) -> None:
    assert penalty_value([0, 2, 4, 8], PenaltyObjective.model_validate(objective)) == pytest.approx(expected)


@pytest.mark.parametrize(
    "policy",
    [
        "random",
        "highest_card",
        "lowest_card",
        "closest_gap",
        "lowest_fitting_card",
        "highest_fitting_card",
        "coldest_row",
        "hand_flexibility",
    ],
)
def test_policy_kernel_is_normalized(view: MatchView, policy: PolicyName) -> None:
    probabilities = [probability(policy, view.rows, view.you.hand, card) for card in view.you.hand]
    assert sum(probabilities) == pytest.approx(1)
    assert probability(policy, view.rows, view.you.hand, 999) == 0


def test_likelihood_uses_continuous_mixture_and_final_card_is_uninformative() -> None:
    assert math.exp(log_likelihood((((1.0,), 3),), 0, float(logit(0.3)))) == pytest.approx(0.8)
    assert math.exp(log_likelihood((((0.0,), 3),), 0, float(logit(0.3)))) == pytest.approx(0.1)
    for value in [-1000.0, 0.0, 1000.0]:
        assert log_likelihood((((1.0,), 1),), 0, value) == pytest.approx(0)


def test_joint_sampler_matches_enumerated_hidden_hand_and_continuous_posterior(options: SimulationOptions) -> None:
    # Card 50 was played from {50} plus two of four unknown cards.
    # For highest_card, exact P(both remaining cards below 50 | play)=4/9,
    # and E[epsilon | play]=5/9 under the uniform priors.
    rows = tuple(RowView(index=i, cards=(v,)) for i, v in enumerate([1, 2, 3, 4]))
    history = HandHistory(rows, (), rows, turns=[ObservedTurn(rows, (("b", 50),))])
    model = options.model.model_copy(update={"policies": ["highest_card"], "mode": "single_policy"})
    posterior = Posterior(history, ("b",), ((),), (2,), [10, 20, 60, 70], model)
    rng = np.random.RandomState(123)
    initial = np.array([np.r_[rng.permutation(posterior.unseen), 0, logit(rng.uniform())] for _ in range(32)])
    sampler = emcee.EnsembleSampler(32, 6, posterior, moves=emcee.moves.MHMove(LegalProposal(4, 1, model)))
    sampler.random_state = rng.get_state()
    sampler.run_mcmc(initial, 1200, skip_initial_state_check=True)
    samples = sampler.get_chain(discard=300, flat=True)
    assert float(np.mean(np.all(samples[:, :2] < 50, axis=1))) == pytest.approx(4 / 9, abs=0.035)
    assert float(np.mean(expit(samples[:, -1]))) == pytest.approx(5 / 9, abs=0.035)


def test_sampled_worlds_conserve_cards_and_are_repeatable(view: MatchView, options: SimulationOptions) -> None:
    history = PublicHistory()
    history.observe(view)
    model = OpponentModel(options.model)
    worlds = model.infer(history, view, random.Random(9))  # noqa: S311 -- reproducible test sampling
    assert worlds == model.infer(history, view, random.Random(9))  # noqa: S311 -- reproducible test sampling
    for world in worlds:
        cards = [
            *view.you.hand,
            *(c for row in view.rows for c in row.cards),
            *(c for hand in world.hands for c in hand),
            *world.undealt,
        ]
        assert sorted(cards) == list(range(1, 105))
        assert [len(hand) for hand in world.hands] == [10, 10]
        assert all(0 < epsilon < 1 for epsilon in world.epsilons)


def test_fixed_mixture_preserves_policy_assignments(options: SimulationOptions) -> None:
    fixed = options.model.model_copy(update={"mode": "fixed_mixture"})
    proposal = LegalProposal(4, 1, fixed)
    coordinates = np.array([[10.0, 20.0, 60.0, 70.0, 1.0, 0.0]])
    rng = np.random.RandomState(8)
    for _ in range(100):
        coordinates, _ = proposal(coordinates, rng)
        assert coordinates[0, 4] == 1


def test_inference_does_not_change_global_numpy_random_state(view: MatchView, options: SimulationOptions) -> None:
    history = PublicHistory()
    history.observe(view)
    before = np.random.get_state()
    OpponentModel(options.model).infer(history, view, random.Random(9))  # noqa: S311 -- reproducible test sampling
    after = np.random.get_state()
    assert before[0] == after[0]
    np.testing.assert_array_equal(before[1], after[1])
    assert before[2:] == after[2:]


def test_history_is_not_counted_twice(view: MatchView, options: SimulationOptions) -> None:
    bot = SimulationBot(4, options, REGISTRY["closest_gap"].build(4))
    first = bot.act(view)
    assert bot.act(view) == first
    assert bot.model.diagnostics["observed_plays"] == 0
    assert len(bot.history.hands) == 1


def test_incomplete_history_uses_configured_fallback(view: MatchView, options: SimulationOptions) -> None:
    fallback = REGISTRY["highest_card"].build(1)
    bot = SimulationBot(4, options, fallback)
    incomplete = view.model_copy(update={"play_number": 2})
    assert bot.act(incomplete) == fallback.act(incomplete)
    assert bot.stats()["recovery_count"] == 1


def test_rollout_resolves_card_order_and_caps_at_end_of_hand(view: MatchView, options: SimulationOptions) -> None:
    rows = tuple(RowView(index=i, cards=cards) for i, cards in enumerate([(10, 11, 12, 13, 14), (30,), (60,), (90,)]))
    small = view.model_copy(
        update={
            "rows": rows,
            "you": PlayerSelfView(player_id="a", hand=(16,)),
            "players": (OpponentView(player_id="b", display_name="b", cards_in_hand=1),),
        }
    )
    world = World(((15,),), (), (0,), (0.0,))
    state = resolve_turn(initial_state(small, world), {"a": 16, "b": 15}, "a", options.row_policy)
    assert state.rows[0].cards == (15, 16)
    assert state.players[0].score_this_hand == 0
    assert state.players[1].penalty_cards == rows[0].cards
    assert all(player.hand == () for player in state.players)
    assert state.hand_number == small.hand_number
    for horizon in [1, 5, "remaining_hand"]:
        configured = options.model_copy(update={"horizon": horizon})
        assert rollout(small, world, 16, configured, 7) == 0


@pytest.mark.parametrize("communication", [False, True])
@pytest.mark.parametrize("name", ["simulation", "model_based_bait"])
def test_bots_finish_games_and_retain_cross_hand_learning(
    options: SimulationOptions, communication: bool, name: str
) -> None:
    bot = REGISTRY[name].build(8, **options.model_dump())
    assert isinstance(bot, SimulationBot)
    result = run_match(
        [bot, REGISTRY["highest_card"].build(2), REGISTRY["random"].build(3)],
        14,
        rules=GameRules(target_score=20),
        protocol=MatchProtocol(communication_enabled=communication),
    )
    assert result.final_state.phase == Phase.FINISHED, result.reason
    assert bot.failures == 0
    assert bot.model.diagnostics["observed_plays"] == result.final_state.hand_number * 10 - 1
    assert len(bot.history.hands) == result.final_state.hand_number


@pytest.mark.parametrize("backend", ["thread", "process"])
def test_seeded_arena_is_repeatable_across_workers(options: SimulationOptions, backend: str) -> None:
    players = [
        PlayerConfig(bot="simulation", options=options.model_dump(mode="json")),
        PlayerConfig(bot="highest_card"),
    ]
    serial = run_arena(players, 2, 72, rules=GameRules(target_score=10))
    concurrent = run_arena(
        players, 2, 72, rules=GameRules(target_score=10), config=RunConfig(backend=backend, concurrency=2)
    )
    assert serial == concurrent
    assert serial.failed == serial.abandoned == serial.forfeited == 0
    assert serial.finished == 2


def test_parallel_inference_uses_independent_rngs(view: MatchView, options: SimulationOptions) -> None:
    bots = [SimulationBot(4, options, REGISTRY["closest_gap"].build(4)) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        actions = list(pool.map(lambda bot: bot.act(view), bots))
    assert all(action == actions[0] for action in actions)
    assert all(bot.stats() == bots[0].stats() for bot in bots)


def test_resolves_nested_fallback_before_construction(options: SimulationOptions) -> None:
    settings = {
        **options.model_dump(mode="json"),
        "fallback_strategy": "controlled_burn",
        "fallback_options": {"K": 2, "fallback_strategy": "highest_card"},
    }
    player = resolve_players([PlayerConfig(bot="simulation", options=settings)])[0]
    assert player.deterministic
    assert player.metadata["fallback_metadata"]["strategy_id"] == "controlled_burn"
    assert isinstance(player.build(3), SimulationBot)


@pytest.mark.parametrize(("mode", "minimum", "maximum"), [("learned_mixture", 0.98, 1.0), ("fixed_mixture", 0.3, 0.7)])
def test_policy_learning_can_be_enabled_or_held_fixed(
    options: SimulationOptions, mode: str, minimum: float, maximum: float
) -> None:
    # Completed hands make historical alternatives known. Eight selections all
    # agree with policy 0 and disagree with policy 1.
    history = HandHistory((), (), ())
    model = options.model.model_copy(update={"mode": mode})
    past = tuple(((1.0, 0.0), size) for size in range(10, 2, -1))
    posterior = Posterior(history, ("b",), (past,), (0,), [], model)
    rng = np.random.RandomState(8)
    coordinates = np.column_stack((rng.randint(2, size=32), logit(rng.uniform(size=32))))
    sampler = emcee.EnsembleSampler(32, 2, posterior, moves=emcee.moves.MHMove(LegalProposal(0, 1, model)))
    sampler.random_state = rng.get_state()
    sampler.run_mcmc(coordinates, 800, skip_initial_state_check=True)
    weights = float(np.mean(sampler.get_chain(discard=400)[:, :, 0] == 0))
    assert minimum <= weights <= maximum


class BatchFallback:
    def __init__(self) -> None:
        self.calls = 0

    def act(self, view: MatchView, rejection=None) -> ActionBatch:
        self.calls += 1
        return ActionBatch((SelectCardAction(card=max(view.you.hand)), CommitAction()), memory="preserve me")


def test_bait_preserves_fallback_batches_without_calling_twice(view: MatchView, options: SimulationOptions) -> None:
    fallback = BatchFallback()
    bait = SimulationBot(3, ModelBasedBaitOptions.model_validate(options.model_dump()), fallback)
    action = bait.act(view)
    assert isinstance(action, ActionBatch)
    assert action.memory == "preserve me"
    assert fallback.calls == 1
    assert bait.failures == 1


def test_bait_keeps_fallback_when_no_full_row_is_targeted(view: MatchView, options: SimulationOptions) -> None:
    fallback = REGISTRY["highest_card"].build(1)
    bot = SimulationBot(3, ModelBasedBaitOptions.model_validate(options.model_dump()), fallback)
    assert bot.act(view) == fallback.act(view)


def test_rollout_accumulates_future_penalties(view: MatchView, options: SimulationOptions) -> None:
    rows = tuple(RowView(index=i, cards=cards) for i, cards in enumerate([(10, 11, 12, 13), (30,), (60,), (90,)]))
    small = view.model_copy(
        update={
            "rows": rows,
            "you": PlayerSelfView(player_id="a", hand=(14, 16)),
            "players": (OpponentView(player_id="b", display_name="b", cards_in_hand=2),),
        }
    )
    world = World(((70, 80),), (), (0,), (0.0,))
    assert rollout(small, world, 14, options, 1) == 0
    assert rollout(small, world, 14, options.model_copy(update={"horizon": 2}), 1) == 11


@pytest.mark.parametrize("policy", ["cheapest", "hand_aware"])
def test_simulated_low_card_uses_configured_actual_row_rule(
    view: MatchView, options: SimulationOptions, policy: Literal["cheapest", "hand_aware"]
) -> None:
    row_options = RowPolicyOptions(policy=policy, max_extra_penalty=2 if policy == "hand_aware" else None)
    bot = SimulationBot(4, options.model_copy(update={"row_policy": row_options}), REGISTRY["closest_gap"].build(4))
    bot.history.observe(view)
    low_view = view.model_copy(
        update={
            "legal_actions": ("choose_row",),
            "awaiting_card": 1,
            "you": view.you.model_copy(update={"hand": (80,)}),
        }
    )
    chosen = bot.act(low_view)
    assert isinstance(chosen, ChooseRowAction)
    state = initial_state(
        view.model_copy(update={"you": view.you.model_copy(update={"hand": (1, 80)}), "players": ()}),
        World((), (), (), ()),
    )
    resolved = resolve_turn(state, {"a": 1}, "a", row_options)
    assert resolved.rows[chosen.row_index].cards == (1,)
