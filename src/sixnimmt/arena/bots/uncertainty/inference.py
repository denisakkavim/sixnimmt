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
from .likelihood import BatchedPosterior
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


class BatchedLegalProposal:
    """Vectorised symmetric moves; emcee still owns MH acceptance."""

    def __init__(self, deck_size: int, opponent_count: int, options: OpponentModelOptions) -> None:
        self.deck_size = deck_size
        self.opponent_count = opponent_count
        self.options = options

    def __call__(
        self, coordinates: NDArray[np.float64], rng: np.random.RandomState
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        proposed = coordinates.copy()
        count = len(proposed)
        moves = rng.randint(3 if self.options.mode == "learned_mixture" else 2, size=count)
        swaps = np.flatnonzero(moves == 0)
        if self.deck_size >= 2:
            first = rng.randint(self.deck_size, size=len(swaps))
            second = rng.randint(self.deck_size - 1, size=len(swaps))
            second += second >= first
            proposed[swaps, first], proposed[swaps, second] = (
                proposed[swaps, second].copy(),
                proposed[swaps, first].copy(),
            )
        epsilon_moves = np.flatnonzero(moves == 1)
        indices = self.deck_size + self.opponent_count + rng.randint(self.opponent_count, size=len(epsilon_moves))
        proposed[epsilon_moves, indices] += rng.normal(
            scale=self.options.epsilon_proposal_scale, size=len(epsilon_moves)
        )
        policy_moves = np.flatnonzero(moves == 2)
        indices = self.deck_size + rng.randint(self.opponent_count, size=len(policy_moves))
        proposed[policy_moves, indices] = rng.randint(len(self.options.policies), size=len(policy_moves))
        return proposed, np.zeros(count)


class OpponentModel:
    def __init__(self, options: OpponentModelOptions) -> None:
        self.options = options
        self.diagnostics: dict = {}
        self._rankings: dict = {}
        self._completed: dict[tuple[int, str], Features] = {}
        self._previous: tuple[str, int, int, tuple[str, ...], list[World]] | None = None

    def infer(self, history: PublicHistory, view: MatchView, rng: random.Random) -> list[World]:
        posterior = self._posterior(history, view)
        # emcee owns a private RandomState; never seed NumPy's process-global RNG.
        sampling_rng = np.random.RandomState(rng.getrandbits(32))
        coordinates, reused = self._initial(posterior, view, sampling_rng)
        observed = sum(len(hand.turns) for hand in history.hands.values())
        if observed == 0:
            # With no behavioural evidence the conditional legal-deal prior is
            # the posterior. No Markov chain or burn-in is needed.
            labels = coordinates[:, len(posterior.unseen) : len(posterior.unseen) + len(posterior.opponent_ids)]
            coordinates = self._prior(posterior, sampling_rng, self.options.particle_count)
            if self.options.mode == "fixed_mixture":
                coordinates[:, len(posterior.unseen) : len(posterior.unseen) + len(posterior.opponent_ids)] = np.tile(
                    labels, (self.options.particle_count // self.options.chain_count, 1)
                )
            worlds = [posterior.world(row) for row in coordinates]
            endpoints = worlds[: self.options.chain_count]
            method = "exact_prior"
        else:
            target = BatchedPosterior(posterior, self._rankings)
            proposal = BatchedLegalProposal(len(posterior.unseen), len(posterior.opponent_ids), self.options)
            sampler = emcee.EnsembleSampler(
                len(coordinates), coordinates.shape[1], target, moves=emcee.moves.MHMove(proposal), vectorize=True
            )
            sampler.random_state = sampling_rng.get_state()
            # Repaired previous worlds are warm starts, not posterior draws.
            # Always run the configured burn-in against the full current target.
            state = sampler.run_mcmc(
                coordinates, self.options.burn_in_steps, skip_initial_state_check=True, store=False
            )
            worlds = []
            for _ in range(self.options.particle_count // self.options.chain_count):
                state = sampler.run_mcmc(state, self.options.draw_interval, skip_initial_state_check=True, store=False)
                worlds.extend(posterior.world(row) for row in state.coords)
            endpoints = [posterior.world(row) for row in state.coords]
            method = "emcee_batched_mh"
        self._previous = (view.match_id, view.hand_number, view.play_number, posterior.opponent_ids, endpoints)
        self.diagnostics = {
            "method": method,
            "particles": len(worlds),
            "chains": self.options.chain_count,
            "burn_in_steps": 0 if observed == 0 else self.options.burn_in_steps,
            "draw_interval": self.options.draw_interval,
            "retained_draws_per_chain": self.options.particle_count // self.options.chain_count,
            "warm_start": reused,
            "observed_plays": observed,
            "opponents": _summaries(worlds, posterior.opponent_ids, self.options),
        }
        return worlds

    def _prior(self, posterior: Posterior, rng: np.random.RandomState, count: int) -> NDArray[np.float64]:
        positions = []
        for _ in range(count):
            deck = rng.permutation(posterior.unseen)
            policies = rng.randint(len(self.options.policies), size=len(posterior.opponent_ids))
            epsilons = rng.uniform(np.nextafter(0.0, 1.0), 1.0, size=len(posterior.opponent_ids))
            positions.append(np.concatenate((deck, policies, logit(epsilons))))
        return np.asarray(positions, dtype=float)

    def _initial(
        self, posterior: Posterior, view: MatchView, rng: np.random.RandomState
    ) -> tuple[NDArray[np.float64], bool]:
        coordinates = self._prior(posterior, rng, self.options.chain_count)
        if self._previous is None:
            return coordinates, False
        match_id, hand, play, opponents, worlds = self._previous
        if match_id != view.match_id or opponents != posterior.opponent_ids:
            msg = "opponent inference state belongs to another match or player roster"
            raise InferenceError(msg)
        if (hand, play) >= (view.hand_number, view.play_number):
            # Never initialise an earlier target from later evidence.
            return coordinates, False
        boundary = len(posterior.unseen)
        count = len(opponents)
        for index, world in enumerate(worlds):
            coordinates[index, boundary : boundary + count] = world.policies
            coordinates[index, boundary + count :] = logit(world.epsilons)
            if hand == view.hand_number:
                coordinates[index, :boundary] = _repair_deal(world, posterior, play - 1, rng)
        return coordinates, True

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
                    key = (number, player.player_id)
                    if key not in self._completed:
                        self._completed[key] = _features(hand, player.player_id, (), self.options)
                    records += self._completed[key]
            past.append(records)
        # Board rankings are needed only for the current hand.
        if self._previous is not None and self._previous[1] != view.hand_number:
            self._rankings.clear()
        return Posterior(
            current, tuple(player.player_id for player in opponents), tuple(past), sizes, unseen, self.options
        )


def _repair_deal(world: World, posterior: Posterior, observed: int, rng: np.random.RandomState) -> list[int]:
    """Construct a legal warm start, never an unweighted posterior update."""
    piles = [list(hand) for hand in world.hands] + [list(world.undealt)]
    for turn in posterior.current.turns[observed:]:
        choices = dict(turn.choices)
        for opponent, player_id in enumerate(posterior.opponent_ids):
            revealed = choices[player_id]
            owner = next(index for index, pile in enumerate(piles) if revealed in pile)
            if owner != opponent:
                slot = int(rng.randint(len(piles[opponent])))
                source = piles[owner].index(revealed)
                piles[owner][source], piles[opponent][slot] = piles[opponent][slot], revealed
            piles[opponent].remove(revealed)
    return [card for pile in piles for card in pile]


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
