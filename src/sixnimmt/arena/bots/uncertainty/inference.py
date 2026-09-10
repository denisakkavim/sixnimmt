"""Joint hidden-hand inference using emcee's Metropolis-Hastings sampler."""

import math
import random
from dataclasses import dataclass, field

import emcee
import numpy as np
from numpy.typing import NDArray
from scipy.special import expit, log_expit, logit

from sixnimmt.engine.views import MatchView

from .history import HandHistory, InferenceError, PublicHistory
from .options import OpponentModelOptions
from .policies import probability

Features = tuple[tuple[tuple[float, ...], int], ...]


@dataclass(frozen=True)
class World:
    hands: tuple[tuple[int, ...], ...]
    undealt: tuple[int, ...]
    policies: tuple[int, ...]
    epsilons: tuple[float, ...]


def log_likelihood(features: Features, policy: int, epsilon_logit: float) -> float:
    """Evaluate the mixture stably, even near epsilon's endpoints."""
    total = 0.0
    for probabilities, size in features:
        preferred = probabilities[policy]
        log_policy = math.log(preferred) if preferred > 0 else -math.inf
        total += float(np.logaddexp(log_expit(-epsilon_logit) + log_policy, log_expit(epsilon_logit) - math.log(size)))
    return total


def _features(hand: HandHistory, player_id: str, remaining: tuple[int, ...], options: OpponentModelOptions) -> Features:
    played = [dict(turn.choices)[player_id] for turn in hand.turns]
    result = []
    for index, turn in enumerate(hand.turns):
        held = tuple(sorted((*remaining, *played[index:])))
        if len(held) != len(set(held)):
            msg = "sampled hand contains duplicate cards"
            raise InferenceError(msg)
        probabilities = tuple(probability(policy, turn.rows, held, played[index]) for policy in options.policies)
        result.append((probabilities, len(held)))
    return tuple(result)


@dataclass
class Posterior:
    current: HandHistory
    opponent_ids: tuple[str, ...]
    past: tuple[Features, ...]
    sizes: tuple[int, ...]
    unseen: list[int]
    options: OpponentModelOptions
    feature_cache: dict[tuple[str, tuple[int, ...]], Features] = field(default_factory=dict)

    def world(self, coordinates: NDArray[np.float64]) -> World:
        count = len(self.opponent_ids)
        boundary = len(self.unseen)
        hands, undealt = _partition([int(card) for card in coordinates[:boundary]], self.sizes)
        policies = tuple(int(index) for index in coordinates[boundary : boundary + count])
        epsilons = tuple(float(value) for value in expit(coordinates[boundary + count :]))
        return World(hands, undealt, policies, epsilons)

    def __call__(self, coordinates: NDArray[np.float64]) -> float:
        world = self.world(coordinates)
        logits = coordinates[len(self.unseen) + len(self.opponent_ids) :]
        # Uniform Beta priors expressed in logit coordinates need this Jacobian.
        value = float(np.sum(log_expit(logits) + log_expit(-logits)))
        for index, player_id in enumerate(self.opponent_ids):
            key = (player_id, world.hands[index])
            if key not in self.feature_cache:
                self.feature_cache[key] = self.past[index] + _features(
                    self.current, player_id, world.hands[index], self.options
                )
            features = self.feature_cache[key]
            value += log_likelihood(features, world.policies[index], float(logits[index]))
        return value


@dataclass(frozen=True)
class LegalProposal:
    """Symmetric proposals on legal deals, policy labels, and epsilon logits."""

    deck_size: int
    opponent_count: int
    options: OpponentModelOptions

    def __call__(
        self, coordinates: NDArray[np.float64], rng: np.random.RandomState
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        proposed = coordinates.copy()
        move_count = 3 if self.options.mode == "learned_mixture" else 2
        for row in proposed:
            move = rng.randint(move_count)
            if move == 0:
                if self.deck_size >= 2:
                    first, second = rng.choice(self.deck_size, size=2, replace=False)
                    row[first], row[second] = row[second], row[first]
            elif move == 1:
                index = self.deck_size + self.opponent_count + rng.randint(self.opponent_count)
                row[index] += rng.normal(scale=self.options.epsilon_proposal_scale)
            else:
                index = self.deck_size + rng.randint(self.opponent_count)
                row[index] = rng.randint(len(self.options.policies))
        # Every forward proposal has the same probability as its reverse.
        return proposed, np.zeros(len(proposed))


class OpponentModel:
    def __init__(self, options: OpponentModelOptions) -> None:
        self.options = options
        self.diagnostics: dict = {}

    def infer(self, history: PublicHistory, view: MatchView, rng: random.Random) -> list[World]:
        posterior = self._posterior(history, view)
        # emcee owns a private RandomState; never seed NumPy's process-global RNG.
        sampling_rng = np.random.RandomState(rng.getrandbits(32))
        positions = []
        for _ in range(self.options.particle_count):
            deck = sampling_rng.permutation(posterior.unseen)
            policies = sampling_rng.randint(len(self.options.policies), size=len(posterior.opponent_ids))
            epsilons = sampling_rng.uniform(np.nextafter(0.0, 1.0), 1.0, size=len(posterior.opponent_ids))
            positions.append(np.concatenate((deck, policies, logit(epsilons))))
        coordinates = np.asarray(positions, dtype=float)
        proposal = LegalProposal(len(posterior.unseen), len(posterior.opponent_ids), self.options)
        sampler = emcee.EnsembleSampler(
            len(positions), coordinates.shape[1], posterior, moves=emcee.moves.MHMove(proposal)
        )
        sampler.random_state = sampling_rng.get_state()
        # Independent MH proposals do not need the rank condition of stretch moves.
        warmed = sampler.run_mcmc(coordinates, self.options.burn_in_steps, skip_initial_state_check=True, store=False)
        # Discard warm-up entirely; retain one subsequent draw per chain.
        final = sampler.run_mcmc(warmed, 1, skip_initial_state_check=True, store=False)
        worlds = [posterior.world(row) for row in final.coords]
        self.diagnostics = {
            "method": "emcee_independent_mh",
            "particles": len(worlds),
            "burn_in_steps": self.options.burn_in_steps,
            "retained_draws_per_chain": 1,
            "observed_plays": sum(len(hand.turns) for hand in history.hands.values()),
            "opponents": _summaries(worlds, posterior.opponent_ids, self.options),
        }
        return worlds

    def _posterior(self, history: PublicHistory, view: MatchView) -> Posterior:
        current = history.current(view)
        opponents = tuple(player for player in view.players if player.player_id != view.you.player_id)
        known = set(current.own_initial_hand)
        known.update(card for row in current.initial_rows for card in row.cards)
        known.update(card for turn in current.turns for _, card in turn.choices)
        unseen = sorted(set(range(1, 105)) - known)
        sizes = tuple(player.cards_in_hand for player in opponents)
        expected_size = 10 - len(current.turns)
        if not opponents or any(size != expected_size for size in sizes) or sum(sizes) > len(unseen):
            msg = "public hand sizes disagree with observed plays"
            raise InferenceError(msg)
        own_played = {dict(turn.choices)[view.you.player_id] for turn in current.turns}
        if set(view.you.hand) != set(current.own_initial_hand) - own_played:
            msg = "own hand disagrees with observed plays"
            raise InferenceError(msg)
        past = []
        for player in opponents:
            records: Features = ()
            for number, hand in sorted(history.hands.items()):
                if number < view.hand_number:
                    records += _features(hand, player.player_id, (), self.options)
            past.append(records)
        return Posterior(
            current, tuple(player.player_id for player in opponents), tuple(past), sizes, unseen, self.options
        )


def _partition(deck: list[int], sizes: tuple[int, ...]) -> tuple[tuple[tuple[int, ...], ...], tuple[int, ...]]:
    offset = 0
    hands = []
    for size in sizes:
        hands.append(tuple(sorted(deck[offset : offset + size])))
        offset += size
    return tuple(hands), tuple(sorted(deck[offset:]))


def _summaries(worlds: list[World], players: tuple[str, ...], options: OpponentModelOptions) -> dict:
    summaries = {}
    for index, player in enumerate(players):
        values = np.array([world.epsilons[index] for world in worlds])
        summaries[player] = {
            "policy_weights": {
                name: sum(world.policies[index] == number for world in worlds) / len(worlds)
                for number, name in enumerate(options.policies)
            },
            "epsilon_mean": float(np.mean(values)),
            "epsilon_interval_90": np.quantile(values, [0.05, 0.95]).tolist(),
        }
    return summaries
