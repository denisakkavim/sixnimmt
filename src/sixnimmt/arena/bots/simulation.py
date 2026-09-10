"""Learn opponent behaviour and evaluate candidate cards by simulation."""

import hashlib
import random
from typing import Any

from sixnimmt.arena.bots._board import applicable_row
from sixnimmt.arena.bots.base import ActionBatch, Bot, Rejection
from sixnimmt.engine.actions import Action, ChooseRowAction, CommitAction, SelectCardAction
from sixnimmt.engine.views import MatchView

from .uncertainty.history import InferenceError, PublicHistory
from .uncertainty.inference import OpponentModel
from .uncertainty.options import ModelBasedBaitOptions, SimulationOptions
from .uncertainty.simulation import penalty_value, rollout, select_row


class SimulationBot:
    def __init__(self, seed: int, options: SimulationOptions, fallback: Bot) -> None:
        self.seed = seed
        self.options = options
        self.fallback = fallback
        self.history = PublicHistory()
        self.model = OpponentModel(options.model)
        self.last_key: tuple[int, int] | None = None
        self.last_card: int | None = None
        self.failures = 0
        self.last_error: str | None = None
        self.estimates: dict[str, float] = {}

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action | ActionBatch:
        try:
            return self._act(view, rejection)
        except InferenceError as error:
            self.failures += 1
            self.last_error = str(error)
            return self.fallback.act(view, rejection)

    def _act(self, view: MatchView, rejection: Rejection | None) -> Action | ActionBatch:
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
        action = self.evaluate(view, rejection)
        if isinstance(action, SelectCardAction):
            self.last_key, self.last_card = key, action.card
        return action

    def evaluate(self, view: MatchView, rejection: Rejection | None) -> Action | ActionBatch:
        seed_bytes = f"{self.seed}:uncertainty:{view.hand_number}:{view.play_number}".encode()
        seed = int.from_bytes(hashlib.sha256(seed_bytes).digest()[:8], "big")
        rng = random.Random(seed)  # noqa: S311 -- private deterministic sampling
        worlds = self.model.infer(self.history, view, rng)
        fallback_card = None
        candidates = sorted(view.you.hand)
        if isinstance(self.options, ModelBasedBaitOptions):
            proposal = self.fallback.act(view, rejection)
            if not isinstance(proposal, SelectCardAction) or proposal.card not in view.you.hand:
                self.failures += 1
                self.last_error = "bait fallback did not return a single legal card selection"
                return proposal
            fallback_card = proposal.card
            candidates = [card for card in candidates if _targets_full_row(card, view)]
            candidates = sorted({*candidates, fallback_card})
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
            "candidate_values": self.estimates,
            "recovery_count": self.failures,
            "last_recovery_reason": self.last_error,
        }

    def __getattr__(self, name: str) -> Any:
        fallback = self.__dict__.get("fallback")
        if fallback is None:
            raise AttributeError(name)
        return getattr(fallback, name)


def _targets_full_row(card: int, view: MatchView) -> bool:
    row = applicable_row(card, view.rows)
    return row is not None and len(row.cards) == 5


def build_simulation(seed: int, **settings: Any) -> SimulationBot:
    from sixnimmt.arena.players import PlayerConfig, resolve_players

    options = SimulationOptions.model_validate(settings)
    fallback = resolve_players([PlayerConfig(bot=options.fallback_strategy, options=options.fallback_options)])[0]
    return SimulationBot(seed, options, fallback.build(seed))


def build_model_based_bait(seed: int, **settings: Any) -> SimulationBot:
    from sixnimmt.arena.players import PlayerConfig, resolve_players

    options = ModelBasedBaitOptions.model_validate(settings)
    fallback = resolve_players([PlayerConfig(bot=options.fallback_strategy, options=options.fallback_options)])[0]
    return SimulationBot(seed, options, fallback.build(seed))
