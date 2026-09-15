"""Batch the exact policy likelihood using bounded, per-hand sufficient statistics."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray
from scipy.special import log_expit

from sixnimmt.arena.bots.uncertainty import inference
from sixnimmt.arena.bots.uncertainty.policies import preferred_card

if TYPE_CHECKING:
    from sixnimmt.arena.bots.uncertainty.inference import Posterior


class BatchedPosterior:
    """The scalar posterior's target, without revisiting completed plays per proposal."""

    def __init__(self, posterior: Posterior, rankings: inference.RankingCache) -> None:
        self.posterior = posterior
        self.boundary = len(posterior.unseen)
        self.count = len(posterior.opponent_ids)
        self.log_sizes = np.log(np.arange(1, 11))
        self.totals = np.zeros(10)
        # Axes: opponent, policy, hand size minus one.
        self.past = np.zeros((self.count, len(posterior.options.policies), 10))
        for opponent, features in enumerate(posterior.past):
            for probabilities, size in features:
                self.past[opponent, :, size - 1] += probabilities
                if opponent == 0:
                    self.totals[size - 1] += 1
        self.turn_sizes = np.array(
            [
                posterior.sizes[0] + len(posterior.current.turns) - index - 1
                for index in range(len(posterior.current.turns))
            ],
            dtype=int,
        )
        for size in self.turn_sizes:
            self.totals[size] += 1
        # Axes: opponent, policy, observed turn, card ID (slot zero unused).
        self.blockers = np.zeros((self.count, len(posterior.options.policies), len(self.turn_sizes), 105), dtype=bool)
        self.future_blocks = np.zeros(self.blockers.shape[:-1], dtype=bool)
        self._prepare(rankings)
        # Cache only the preceding coordinates; invalidate on any shape change.
        self.previous: NDArray[np.float64] | None = None
        self.values = np.empty((0, self.count))

    def _prepare(self, rankings: inference.RankingCache) -> None:
        current = self.posterior.current
        for opponent, player_id in enumerate(self.posterior.opponent_ids):
            played = [dict(turn.choices)[player_id] for turn in current.turns]
            for turn_index, turn in enumerate(current.turns):
                chosen = played[turn_index]
                for policy_index, policy in enumerate(self.posterior.options.policies):
                    if policy in {"random", "hand_flexibility"}:
                        continue
                    key = inference.RankingKey(policy, tuple(row.cards for row in turn.rows), chosen)
                    if key not in rankings:
                        # These policies order individual cards independently of
                        # the other cards held. Ask the real bot to preserve ties.
                        rankings[key] = np.array([
                            card != chosen and preferred_card(policy, turn.rows, (chosen, card)) != chosen
                            for card in range(1, 105)
                        ])
                    blocked = self.blockers[opponent, policy_index, turn_index]
                    blocked[1:] = rankings[key]
                    self.future_blocks[opponent, policy_index, turn_index] = np.any(blocked[played[turn_index + 1 :]])

    def __call__(self, coordinates: NDArray[np.float64]) -> NDArray[np.float64]:
        if self.previous is None or self.previous.shape != coordinates.shape:
            self.previous = np.full_like(coordinates, np.nan)
            self.values = np.zeros((len(coordinates), self.count))
        hand_boundary = sum(self.posterior.sizes)
        hands = coordinates[:, :hand_boundary].reshape(len(coordinates), self.count, self.posterior.sizes[0])
        previous_hands = self.previous[:, :hand_boundary].reshape(hands.shape)
        labels = coordinates[:, self.posterior.layout.policies]
        logits = coordinates[:, self.posterior.layout.logits]
        changed = np.any(hands != previous_hands, axis=2)
        changed |= labels != self.previous[:, self.posterior.layout.policies]
        changed |= logits != self.previous[:, self.posterior.layout.logits]
        chains, opponents = np.nonzero(changed)
        if len(chains) > 0:
            self.values[chains, opponents] = self._opponents(
                hands[chains, opponents].astype(int),
                labels[chains, opponents].astype(int),
                logits[chains, opponents],
                opponents,
            )
        self.previous = coordinates.copy()
        return np.sum(self.values, axis=1)

    def _opponents(
        self,
        hands: NDArray[np.int64],
        labels: NDArray[np.int64],
        logits: NDArray[np.float64],
        opponents: NDArray[np.int64],
    ) -> NDArray[np.float64]:
        successes = self.past[opponents, labels].copy()
        if len(self.turn_sizes) > 0:
            blocked = self.blockers[
                opponents[:, None, None],
                labels[:, None, None],
                np.arange(len(self.turn_sizes))[None, :, None],
                hands[:, None, :],
            ]
            agrees = ~(np.any(blocked, axis=2) | self.future_blocks[opponents, labels])
            successes[:, self.turn_sizes] += agrees
        for policy_index, policy in enumerate(self.posterior.options.policies):
            selected = labels == policy_index
            if policy == "random":
                # A uniform policy is independent of epsilon, including its prior.
                successes[selected] = 0
            elif policy == "hand_flexibility" and np.any(selected):
                self._flexibility(successes, hands, selected, opponents, policy_index)
        log_epsilon = log_expit(logits)
        log_policy = log_expit(-logits)
        log_mistake = log_epsilon[:, None] - self.log_sizes
        log_agreement = np.logaddexp(log_policy[:, None], log_mistake)
        likelihood = np.sum(successes * log_agreement + (self.totals - successes) * log_mistake, axis=1)
        for policy_index, policy in enumerate(self.posterior.options.policies):
            if policy == "random":
                likelihood[labels == policy_index] = -float(np.dot(self.totals, self.log_sizes))
        # Uniform Beta priors in logit coordinates include the Jacobian.
        return likelihood + log_epsilon + log_policy

    def _flexibility(
        self,
        successes: NDArray[np.float64],
        hands: NDArray[np.int64],
        selected: NDArray[np.bool_],
        opponents: NDArray[np.int64],
        policy: int,
    ) -> None:
        for index in np.flatnonzero(selected):
            opponent = int(opponents[index])
            player_id = self.posterior.opponent_ids[opponent]
            key = inference.FeatureKey(player_id, tuple(sorted(int(card) for card in hands[index])))
            if key not in self.posterior.feature_cache:
                self.posterior.feature_cache[key] = self.posterior.past[opponent] + inference._features(
                    self.posterior.current, player_id, key.remaining_hand, self.posterior.options
                )
            successes[index] = 0
            for probabilities, size in self.posterior.feature_cache[key]:
                successes[index, size - 1] += probabilities[policy]
