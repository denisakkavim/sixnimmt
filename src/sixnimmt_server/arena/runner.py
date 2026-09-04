"""Run classic matches without HTTP, with isolated seeds and bounded bot calls."""

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sixnimmt_server.arena.bots import Bot, BotObservation, RandomBot
from sixnimmt_server.engine.actions import ChooseRowAction, SelectCardAction
from sixnimmt_server.engine.events import Event
from sixnimmt_server.engine.rules import GameRules, MatchProtocol
from sixnimmt_server.engine.setup import create_match
from sixnimmt_server.engine.state import MatchState, Phase
from sixnimmt_server.engine.transition import transition
from sixnimmt_server.engine.views import RowView

Observer = Callable[[MatchState, tuple[Event, ...]], None]
DEFAULT_MAX_ACTIONS = 10_000


class ArenaError(RuntimeError):
    """A bot failed or a match could not complete within its action limit."""


@dataclass(frozen=True)
class MatchResult:
    seed: int
    final_state: MatchState
    winners: tuple[str, ...]
    actions: int


@dataclass(frozen=True)
class SeatResult:
    player_id: str
    bot_name: str
    wins: int
    ties: int
    total_score: int


@dataclass(frozen=True)
class ArenaResult:
    seed: int
    games: int
    total_hands: int
    total_actions: int
    players: tuple[SeatResult, ...]


def derive_seed(seed: int, domain: str, game_index: int, seat_index: int | None = None) -> int:
    """Stable, independent streams; indices are zero-based, never RNG draw counts."""
    value = f"sixnimmt-arena:{seed}:{domain}:{game_index}:{seat_index}"
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")


def _validate_player_count(count: int) -> None:
    if not 2 <= count <= 10:
        msg = "an arena match requires between 2 and 10 players"
        raise ValueError(msg)


def _acting_seat(state: MatchState) -> int:
    if state.phase == Phase.SELECTING:
        for index, player in enumerate(state.players):
            if not player.committed:
                return index
    elif state.phase == Phase.AWAITING_ROW_CHOICE and state.resolution is not None:
        for index, player in enumerate(state.players):
            if player.player_id == state.resolution.awaiting_player:
                return index
    msg = f"no bot decision available during {state.phase.value}"
    raise ArenaError(msg)


def observe_player(state: MatchState, player_id: str) -> BotObservation:
    """Build only the information needed for a scheduled classic decision."""
    player = next(player for player in state.players if player.player_id == player_id)
    return BotObservation(
        player_id=player.player_id,
        decision="choose_row" if state.phase == Phase.AWAITING_ROW_CHOICE else "select_card",
        hand=player.hand,
        rows=tuple(RowView(index=row.index, cards=row.cards) for row in state.rows),
        hand_number=state.hand_number,
        play_number=state.play_number,
    )


def _bot_action(bot: Bot, observation: BotObservation) -> SelectCardAction | ChooseRowAction:
    action = bot.act(observation)
    if not isinstance(action, (SelectCardAction, ChooseRowAction)):
        msg = "classic bots must return select_card or choose_row actions"
        raise TypeError(msg)
    return action


def _context(state: MatchState, seed: int, player_id: str | None = None) -> str:
    return (
        f"match {state.match_id}, seed {seed}, player {player_id}, "
        f"hand {state.hand_number}, play {state.play_number}, phase {state.phase.value}"
    )


def run_match(
    bots: Sequence[Bot],
    seed: int,
    *,
    match_id: str = "arena_0",
    observer: Observer | None = None,
    max_actions: int = DEFAULT_MAX_ACTIONS,
) -> MatchResult:
    """Run trusted, terminating bot callbacks; the action limit is not a sandbox.

    The observer is privileged test/instrumentation code and receives authoritative
    state. Bots receive only BotObservation. Neither callbacks nor logs are retained.
    """
    _validate_player_count(len(bots))
    if max_actions < 1:
        msg = "max_actions must be positive"
        raise ValueError(msg)
    player_ids = [f"player_{index + 1}" for index in range(len(bots))]
    state, events = create_match(match_id, player_ids, seed)
    rules = GameRules()
    protocol = MatchProtocol()
    if observer is not None:
        observer(state, tuple(events))
    actions = 0
    while state.phase != Phase.FINISHED:
        if actions >= max_actions:
            msg = f"action limit {max_actions} exhausted ({_context(state, seed)})"
            raise ArenaError(msg)
        player_id: str | None = None
        try:
            seat = _acting_seat(state)
            player_id = state.players[seat].player_id
            observation = observe_player(state, player_id)
            action = _bot_action(bots[seat], observation)
            state, events = transition(state, player_id, action, protocol, rules)
        except Exception as error:
            msg = f"bot decision failed ({_context(state, seed, player_id)}): {error}"
            raise ArenaError(msg) from error
        actions += 1
        if observer is not None:
            observer(state, tuple(events))
    lowest = min(player.total_score for player in state.players)
    winners = tuple(sorted(player.player_id for player in state.players if player.total_score == lowest))
    return MatchResult(seed=seed, final_state=state, winners=winners, actions=actions)


def run_arena(
    players: list[str],
    games: int,
    seed: int,
    *,
    max_actions_per_match: int = DEFAULT_MAX_ACTIONS,
    observer: Observer | None = None,
) -> ArenaResult:
    """Run fresh random bots per game, retaining only per-seat aggregate counters."""
    _validate_player_count(len(players))
    if games < 1:
        msg = "games must be positive"
        raise ValueError(msg)
    if max_actions_per_match < 1:
        msg = "max_actions_per_match must be positive"
        raise ValueError(msg)
    for name in players:
        if name != "random":
            msg = f"unknown bot {name!r}; available bots: random"
            raise ValueError(msg)
    wins = [0] * len(players)
    ties = [0] * len(players)
    scores = [0] * len(players)
    total_hands = 0
    total_actions = 0
    for game_index in range(games):
        match_seed = derive_seed(seed, "match", game_index)
        bots = [RandomBot(derive_seed(seed, "bot", game_index, seat)) for seat in range(len(players))]
        result = run_match(
            bots,
            match_seed,
            match_id=f"arena_{game_index}",
            observer=observer,
            max_actions=max_actions_per_match,
        )
        total_hands += result.final_state.hand_number
        total_actions += result.actions
        for seat, player in enumerate(result.final_state.players):
            scores[seat] += player.total_score
            if player.player_id in result.winners:
                if len(result.winners) == 1:
                    wins[seat] += 1
                else:
                    ties[seat] += 1
    seats = tuple(
        SeatResult(
            player_id=f"player_{seat + 1}",
            bot_name=name,
            wins=wins[seat],
            ties=ties[seat],
            total_score=scores[seat],
        )
        for seat, name in enumerate(players)
    )
    return ArenaResult(seed, games, total_hands, total_actions, seats)
