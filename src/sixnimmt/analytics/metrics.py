"""Exact credits for random tie breaking, shared by trace and run analysis."""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class FinishCredits:
    win_credit: float
    acceptable_credit: float
    finishing_distribution: tuple[float, ...]
    sole_win: bool
    shared_first: bool


def finish_credits(scores: Sequence[int], seat: int, acceptable_positions: int) -> FinishCredits:
    """Distribute a tied score uniformly over the places occupied by its tie."""
    if len(scores) < 2 or seat < 0 or seat >= len(scores):
        msg = "scores must contain the requested seat and at least two players"
        raise ValueError(msg)
    if acceptable_positions < 1 or acceptable_positions > len(scores):
        msg = "acceptable positions must be between one and the player count"
        raise ValueError(msg)
    score = scores[seat]
    ahead = sum(other < score for other in scores)
    tied = sum(other == score for other in scores)
    distribution = tuple(1 / tied if ahead <= place < ahead + tied else 0.0 for place in range(len(scores)))
    return FinishCredits(
        win_credit=1 / tied if ahead == 0 else 0.0,
        acceptable_credit=max(0, min(tied, acceptable_positions - ahead)) / tied,
        finishing_distribution=distribution,
        sole_win=ahead == 0 and tied == 1,
        shared_first=ahead == 0 and tied > 1,
    )
