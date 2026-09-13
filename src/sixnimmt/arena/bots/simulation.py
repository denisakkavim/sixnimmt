"""Learn opponent behaviour and evaluate candidate cards by simulation."""

import hashlib
import random
from typing import Any

from sixnimmt.arena.bots.base import ActionBatch, Bot, Rejection
from sixnimmt.arena.bots.heuristics import applicable_row
from sixnimmt.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.views import MatchView

from .uncertainty.history import InferenceError, PublicHistory
from .uncertainty.inference import OpponentModel
from .uncertainty.options import ModelBasedBaitOptions, SimulationOptions
from .uncertainty.rollouts import penalty_value, rollout, select_row


class SimulationBot:
    def __init__(self, seed: int, options: SimulationOptions | ModelBasedBaitOptions) -> None:
        self.seed = seed
        self.options = options
        self.history = PublicHistory()
        self.model = OpponentModel(options.model)
        self.last_key: tuple[int, int] | None = None
        self.last_card: int | None = None
        self.estimates: dict[str, float] = {}
        self.last_evaluation: str | None = None

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action | ActionBatch:
        self.history.observe(view)
        if "choose_row" in view.legal_actions:
            if view.awaiting_card is None:
                msg = "row choice has no awaiting card"
                raise InferenceError(msg)
            return ChooseRowAction(
                row_index=select_row(view.rows, view.you.hand, view.awaiting_card, self.options.row_policy)
            )
        if "commit" in view.legal_actions:
            return CommitAction()
        key = (view.hand_number, view.play_number)
        if key == self.last_key and self.last_card in view.you.hand:
            return SelectCardAction(card=self.last_card)
        if len(view.you.hand) == 1:
            self.last_evaluation = "only_legal_card"
            self.estimates = {}
            return SelectCardAction(card=view.you.hand[0])
        action = self.evaluate(view, rejection)
        if isinstance(action, SelectCardAction):
            self.last_key, self.last_card = key, action.card
        return action

    def evaluate(self, view: MatchView, rejection: Rejection | None) -> Action | ActionBatch:
        return self._evaluate_candidates(view, sorted(view.you.hand), None)

    def _evaluate_candidates(
        self, view: MatchView, candidates: list[int], fallback_card: int | None
    ) -> SelectCardAction:
        seed_bytes = f"{self.seed}:uncertainty:{view.hand_number}:{view.play_number}".encode()
        seed = int.from_bytes(hashlib.sha256(seed_bytes).digest()[:8], "big")
        rng = random.Random(seed)  # noqa: S311 -- private deterministic sampling
        self.estimates = {}
        if len(candidates) == 1:
            self.last_evaluation = "only_candidate"
            return SelectCardAction(card=candidates[0])
        worlds = self.model.infer(self.history, view, rng)
        self.last_evaluation = "simulation"
        samples: dict[int, list[int]] = {card: [] for card in candidates}
        for _ in range(self.options.sample_count):
            world = rng.choice(worlds)
            rollout_seed = rng.getrandbits(64)
            for card in candidates:
                samples[card].append(rollout(view, world, card, self.options, rollout_seed))
        scores = {card: penalty_value(values, self.options.objective) for card, values in samples.items()}
        self.estimates = {str(card): value for card, value in scores.items()}
        best = min(candidates, key=lambda card: (scores[card], card))
        if fallback_card is not None and scores[fallback_card] <= scores[best]:
            return SelectCardAction(card=fallback_card)
        return SelectCardAction(card=best)

    def stats(self) -> dict:
        return {
            "model": self.model.diagnostics,
            "evaluation": self.last_evaluation,
            "candidate_values": self.estimates,
        }


class ModelBasedBaitBot(SimulationBot):
    def __init__(self, seed: int, options: ModelBasedBaitOptions, fallback: Bot) -> None:
        super().__init__(seed, options)
        self.fallback = fallback

    def evaluate(self, view: MatchView, rejection: Rejection | None) -> Action | ActionBatch:
        proposal = self.fallback.act(view, rejection)
        if not isinstance(proposal, SelectCardAction) or proposal.card not in view.you.hand:
            self.estimates = {}
            self.last_evaluation = "fallback_proposal"
            return proposal
        candidates = [card for card in view.you.hand if _targets_full_row(card, view)]
        return self._evaluate_candidates(view, sorted({*candidates, proposal.card}), proposal.card)

    def __getattr__(self, name: str) -> Any:
        fallback = self.__dict__.get("fallback")
        if fallback is None:
            raise AttributeError(name)
        return getattr(fallback, name)


def _targets_full_row(card: int, view: MatchView) -> bool:
    row = applicable_row(card, view.rows)
    return row is not None and len(row.cards) == 5


def build_simulation(seed: int, **settings: Any) -> SimulationBot:
    return SimulationBot(seed, SimulationOptions.model_validate(settings))
