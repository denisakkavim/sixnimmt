"""Joint hidden-hand inference using emcee's Metropolis-Hastings sampler."""

import math
import random
from dataclasses import dataclass, field
from typing import Literal, NamedTuple, TypedDict

import numpy as np
from numpy.typing import NDArray
from scipy.special import expit, log_expit, logit

from sixnimmt.arena.bots.uncertainty.history import HandHistory, InferenceError, PublicHistory
from sixnimmt.arena.bots.uncertainty.likelihood import BatchedPosterior
from sixnimmt.arena.bots.uncertainty.options import OpponentModelOptions, PolicyName
from sixnimmt.arena.bots.uncertainty.policies import probability
from sixnimmt.arena.bots.uncertainty.sampler import PosteriorSampler
from sixnimmt.engine.views import MatchView


class ChoiceFeatures(NamedTuple):
    """Likelihood of the observed choice under each policy, and hand size."""

    probabilities: tuple[float, ...]
    hand_size: int


Features = tuple[ChoiceFeatures, ...]


class FeatureKey(NamedTuple):
    player_id: str
    remaining_hand: tuple[int, ...]


class CompletedHandKey(NamedTuple):
    hand_number: int
    player_id: str


class RankingKey(NamedTuple):
    policy: PolicyName
    rows: tuple[tuple[int, ...], ...]
    chosen_card: int


RankingCache = dict[RankingKey, NDArray[np.bool_]]


@dataclass(frozen=True)
class CoordinateLayout:
    """One chain row: permuted unseen deck, opponent policy labels, epsilon logits.

    Deck slots partition into opponent hands in roster order, then undealt cards.
    Batched coordinates have axes (chain, coordinate); labels/logits each have one
    coordinate per opponent. Card IDs are 1..104; zero is never a deck entry.
    """

    deck_size: int
    opponent_count: int

    @property
    def deck(self) -> slice:
        return slice(0, self.deck_size)

    @property
    def policies(self) -> slice:
        return slice(self.deck_size, self.deck_size + self.opponent_count)

    @property
    def logits(self) -> slice:
        return slice(self.deck_size + self.opponent_count, None)


@dataclass(frozen=True)
class World:
    hands: tuple[tuple[int, ...], ...]
    undealt: tuple[int, ...]
    policies: tuple[int, ...]
    epsilons: tuple[float, ...]


class PreviousInference(NamedTuple):
    match_id: str
    hand_number: int
    play_number: int
    opponent_ids: tuple[str, ...]
    endpoints: tuple[World, ...]


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
        result.append(ChoiceFeatures(probabilities, len(held)))
    return tuple(result)


@dataclass
class Posterior:
    current: HandHistory
    opponent_ids: tuple[str, ...]
    past: tuple[Features, ...]
    sizes: tuple[int, ...]
    unseen: list[int]
    options: OpponentModelOptions
    # Scoped to this posterior: keys are valid only for its current hand/history.
    feature_cache: dict[FeatureKey, Features] = field(default_factory=dict)

    layout: CoordinateLayout = field(init=False)

    def __post_init__(self) -> None:
        self.layout = CoordinateLayout(len(self.unseen), len(self.opponent_ids))

    def world(self, coordinates: NDArray[np.float64]) -> World:
        hands, undealt = _partition([int(card) for card in coordinates[self.layout.deck]], self.sizes)
        policies = tuple(int(index) for index in coordinates[self.layout.policies])
        epsilons = tuple(float(value) for value in expit(coordinates[self.layout.logits]))
        return World(hands, undealt, policies, epsilons)

    def __call__(self, coordinates: NDArray[np.float64]) -> float:
        world = self.world(coordinates)
        logits = coordinates[self.layout.logits]
        # Uniform Beta priors expressed in logit coordinates need this Jacobian.
        value = float(np.sum(log_expit(logits) + log_expit(-logits)))
        for index, player_id in enumerate(self.opponent_ids):
            key = FeatureKey(player_id, world.hands[index])
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


class OpponentSummary(TypedDict):
    policy_weights: dict[PolicyName, float]
    epsilon_mean: float
    epsilon_interval_90: list[float]


class InferenceDiagnostics(TypedDict, total=False):
    method: Literal["exact_prior", "emcee_batched_mh"]
    particles: int
    chains: int
    burn_in_steps: int
    draw_interval: int
    retained_draws_per_chain: int
    warm_start: bool
    observed_plays: int
    opponents: dict[str, OpponentSummary]


class OpponentModel:
    def __init__(self, options: OpponentModelOptions) -> None:
        self.options = options
        self.diagnostics: InferenceDiagnostics = {}
        self._rankings: RankingCache = {}
        self._completed: dict[CompletedHandKey, Features] = {}
        self._previous: PreviousInference | None = None

    def infer(self, history: PublicHistory, view: MatchView, rng: random.Random) -> list[World]:
        posterior = self._posterior(history, view)
        # emcee owns a private RandomState; never seed NumPy's process-global RNG.
        sampling_rng = np.random.RandomState(rng.getrandbits(32))
        coordinates, reused = self._initial(posterior, view, sampling_rng)
        observed = sum(len(hand.turns) for hand in history.hands.values())
        if observed == 0:
            # With no behavioural evidence the conditional legal-deal prior is
            # the posterior. No Markov chain or burn-in is needed.
            labels = coordinates[:, posterior.layout.policies]
            coordinates = self._prior(posterior, sampling_rng, self.options.particle_count)
            if self.options.mode == "fixed_mixture":
                coordinates[:, posterior.layout.policies] = np.tile(
                    labels, (self.options.particle_count // self.options.chain_count, 1)
                )
            worlds = [posterior.world(row) for row in coordinates]
            endpoints = worlds[: self.options.chain_count]
            method = "exact_prior"
        else:
            target = BatchedPosterior(posterior, self._rankings)
            proposal = BatchedLegalProposal(len(posterior.unseen), len(posterior.opponent_ids), self.options)
            sampler = PosteriorSampler(coordinates, target, proposal, sampling_rng)
            # Repaired previous worlds are warm starts, not posterior draws.
            # Always run the configured burn-in against the full current target.
            coordinates = sampler.advance(coordinates, self.options.burn_in_steps)
            worlds = []
            for _ in range(self.options.particle_count // self.options.chain_count):
                coordinates = sampler.advance(coordinates, self.options.draw_interval)
                worlds.extend(posterior.world(row) for row in coordinates)
            endpoints = [posterior.world(row) for row in coordinates]
            method = "emcee_batched_mh"
        self._previous = PreviousInference(
            view.match_id, view.hand_number, view.play_number, posterior.opponent_ids, tuple(endpoints)
        )
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
        previous = self._previous
        if previous.match_id != view.match_id or previous.opponent_ids != posterior.opponent_ids:
            msg = "opponent inference state belongs to another match or player roster"
            raise InferenceError(msg)
        if (previous.hand_number, previous.play_number) >= (view.hand_number, view.play_number):
            # Never initialise an earlier target from later evidence.
            return coordinates, False
        for index, world in enumerate(previous.endpoints):
            coordinates[index, posterior.layout.policies] = world.policies
            coordinates[index, posterior.layout.logits] = logit(world.epsilons)
            if previous.hand_number == view.hand_number:
                coordinates[index, posterior.layout.deck] = _repair_deal(
                    world, posterior, previous.play_number - 1, rng
                )
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
        if len(opponents) == 0 or any(size != expected_size for size in sizes) or sum(sizes) > len(unseen):
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
                    key = CompletedHandKey(number, player.player_id)
                    if key not in self._completed:
                        self._completed[key] = _features(hand, player.player_id, (), self.options)
                    records += self._completed[key]
            past.append(records)
        # Board rankings are needed only for the current hand.
        if self._previous is not None and self._previous.hand_number != view.hand_number:
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


def _summaries(
    worlds: list[World], players: tuple[str, ...], options: OpponentModelOptions
) -> dict[str, OpponentSummary]:
    summaries: dict[str, OpponentSummary] = {}
    for index, player in enumerate(players):
        values = np.array([world.epsilons[index] for world in worlds])
        summaries[player] = {
            "policy_weights": {
                name: sum(world.policies[index] == number for world in worlds) / len(worlds)
                for number, name in enumerate(options.policies)
            },
            "epsilon_mean": float(np.mean(values)),
            "epsilon_interval_90": [float(value) for value in np.quantile(values, [0.05, 0.95])],
        }
    return summaries
