"""Trusted bot interface: decisions use only a player's own cards and public rows."""

import random
from dataclasses import dataclass
from typing import Literal, Protocol

from sixnimmt_server.engine.actions import ChooseRowAction, SelectCardAction
from sixnimmt_server.engine.views import RowView


@dataclass(frozen=True)
class BotObservation:
    player_id: str
    decision: Literal["select_card", "choose_row"]
    hand: tuple[int, ...]
    rows: tuple[RowView, ...]
    hand_number: int
    play_number: int


class Bot(Protocol):
    def act(self, observation: BotObservation) -> SelectCardAction | ChooseRowAction: ...


class RandomBot:
    """Choose uniformly among the observed hand or rows, with a private RNG."""

    def __init__(self, seed: int) -> None:
        self._random = random.Random(seed)  # noqa: S311 - reproducible game choices, not secrets

    def act(self, observation: BotObservation) -> SelectCardAction | ChooseRowAction:
        if observation.decision == "select_card":
            return SelectCardAction(card=self._random.choice(observation.hand))
        row = self._random.choice(observation.rows)
        return ChooseRowAction(row_index=row.index)
