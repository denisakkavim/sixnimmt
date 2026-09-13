"""Aggregate worker summaries without retaining game histories."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sixnimmt.arena.results import ArenaResult, MatchOutcome, MatchResult, SeatResult


@dataclass(frozen=True)
class _GameSummary:
    """Only the counters needed by the parent; histories stay in the worker."""

    outcome: MatchOutcome
    hands: int
    actions: int
    winners: tuple[str, ...]
    player_scores: tuple[tuple[str, int], ...]
    seat_actions: tuple[tuple[int, int], ...]

    @classmethod
    def from_result(cls, result: MatchResult) -> "_GameSummary":
        return cls(
            result.outcome,
            result.final_state.hand_number,
            result.actions,
            result.winners,
            tuple((player.player_id, player.total_score) for player in result.final_state.players),
            result.seat_actions,
        )


class _Aggregate:
    def __init__(self, players: Sequence[str], display_names: Sequence[str]) -> None:
        self.players = players
        self.display_names = display_names
        self.counts = dict.fromkeys(MatchOutcome, 0)
        self.hands = 0
        self.actions = 0
        self.wins = [0] * len(players)
        self.ties = [0] * len(players)
        self.scores = [0] * len(players)
        self.accepted = [0] * len(players)
        self.rejected = [0] * len(players)

    def add(self, result: _GameSummary, on_progress: Callable[[int], None] | None) -> None:
        self.counts[result.outcome] += 1
        self.hands += result.hands
        self.actions += result.actions
        for index, (player_id, score) in enumerate(result.player_scores):
            accepted, rejected = result.seat_actions[index]
            self.accepted[index] += accepted
            self.rejected[index] += rejected
            if result.outcome != MatchOutcome.FINISHED:
                continue
            self.scores[index] += score
            if player_id in result.winners:
                if len(result.winners) == 1:
                    self.wins[index] += 1
                else:
                    self.ties[index] += 1

        if on_progress is not None:
            on_progress(sum(self.counts.values()))

    def result(
        self, run_id: str, seed: int, games: int, started: int, abandoned: int, reproducible: bool
    ) -> ArenaResult:
        seats = tuple(
            SeatResult(
                f"player_{i + 1}",
                name,
                self.wins[i],
                self.ties[i],
                self.scores[i],
                self.accepted[i],
                self.rejected[i],
                self.display_names[i],
            )
            for i, name in enumerate(self.players)
        )
        return ArenaResult(
            run_id,
            seed,
            games,
            started,
            sum(self.counts.values()),
            self.counts[MatchOutcome.FINISHED],
            self.counts[MatchOutcome.ABANDONED],
            self.counts[MatchOutcome.FORFEITED],
            self.counts[MatchOutcome.FAILED],
            abandoned,
            self.hands,
            self.actions,
            reproducible,
            seats,
        )
