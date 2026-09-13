"""Compare archived and current simulation decisions on identical public observations."""

import argparse
import copy
import hashlib
import importlib.metadata
import importlib.util
import json
import platform
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from experiments.mcmc.diagnose import CATALOGUE, ControlledOpponent
from sixnimmt.arena.bots import REGISTRY
from sixnimmt.arena.bots.base import ActionBatch, Rejection
from sixnimmt.arena.bots.simulation import SimulationBot
from sixnimmt.arena.bots.uncertainty.history import PublicHistory
from sixnimmt.arena.bots.uncertainty.inference import OpponentModel, World
from sixnimmt.arena.bots.uncertainty.options import SimulationOptions
from sixnimmt.arena.runner import run_match
from sixnimmt.engine.actions import Action
from sixnimmt.engine.rules import GameRules
from sixnimmt.engine.views import MatchView

CASES = [(3, 1, 1), (3, 1, 5), (3, 1, 9), (3, 3, 5), (5, 1, 5), (10, 3, 5)]


class Recorder:
    def __init__(self) -> None:
        self.history = PublicHistory()
        self.snapshots: dict[tuple[int, int], tuple[PublicHistory, MatchView]] = {}
        self.bot = REGISTRY["closest_gap"].build(1)

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action | ActionBatch:
        self.history.observe(view)
        key = (view.hand_number, view.play_number)
        if key not in self.snapshots and "select_card" in view.legal_actions:
            self.snapshots[key] = (copy.deepcopy(self.history), view)
        return self.bot.act(view, rejection)


def capture(
    players: int, noise: float = 0.0, seed: int = 123
) -> dict[tuple[int, int], tuple[PublicHistory, MatchView]]:
    recorder = Recorder()
    bots = [recorder, *[ControlledOpponent(CATALOGUE[i % 3], noise, seed + i + 1) for i in range(players - 1)]]
    run_match(bots, seed, rules=GameRules(target_score=10000), max_actions=players * 10 * 3 + 20)
    return recorder.snapshots


class TimedModel(OpponentModel):
    def __init__(self, model: Any) -> None:
        super().__init__(model.options)
        self.model = model
        self.seconds = 0.0

    def infer(self, history: PublicHistory, view: MatchView, rng: random.Random) -> list[World]:
        start = time.perf_counter()
        result = self.model.infer(history, view, rng)
        self.seconds = time.perf_counter() - start
        self.diagnostics = self.model.diagnostics
        return result


def load_baseline(root: Path, name: str, relative: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, root / "src/sixnimmt/arena/bots" / relative)
    if spec is None or spec.loader is None:
        msg = "cannot load archived implementation"
        raise ValueError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def measure(bot: Any, history: PublicHistory, view: MatchView) -> dict:
    bot.history = copy.deepcopy(history)
    bot.model.seconds = 0.0
    started = time.perf_counter()
    action = bot.act(view)
    seconds = time.perf_counter() - started
    if getattr(bot, "failures", 0) != 0:
        raise RuntimeError(bot.last_error)
    return {
        "seconds": seconds,
        "inference_seconds": bot.model.seconds,
        "card": action.card,
        "candidate_values": bot.estimates,
        "diagnostics": bot.model.diagnostics,
    }


def regular_times(view: MatchView) -> dict:
    timings = {}
    for name in ["random", "lowest_card", "closest_gap", "hand_flexibility"]:
        bot = REGISTRY[name].build(123)
        batches = []
        for _ in range(5):
            started = time.perf_counter()
            for _ in range(1000):
                bot.act(view)
            batches.append((time.perf_counter() - started) / 1000)
        timings[name] = statistics.median(batches)
    return timings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--baseline-revision")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--reuse-before", type=Path)
    args = parser.parse_args()
    previous = json.loads(args.reuse_before.read_text()) if args.reuse_before is not None else None
    old_model = load_baseline(
        args.baseline_root, "sixnimmt.arena.bots.uncertainty._old_inference", "uncertainty/inference.py"
    ).OpponentModel
    old_bot = load_baseline(args.baseline_root, "sixnimmt.arena.bots._old_simulation", "simulation.py").SimulationBot
    settings = json.loads(Path("examples/arena-uncertainty-players.json").read_text())[0]["options"]
    options = SimulationOptions.model_validate(settings)
    matched = options.model_copy(
        update={
            "model": options.model.model_validate({
                **options.model.model_dump(),
                **{"chain_count": options.model.particle_count, "draw_interval": 1},
            })
        }
    )
    retained = options.model_copy(
        update={
            "model": options.model.model_validate({
                **options.model.model_dump(),
                **{"chain_count": 8, "draw_interval": 100},
            })
        }
    )
    trajectories = {players: capture(players) for players in [3, 5, 10]}
    result = {
        "platform": platform.platform(),
        "baseline_revision": args.baseline_revision,
        "source_sha256": {
            name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            for name in [
                "src/sixnimmt/arena/bots/simulation.py",
                "src/sixnimmt/arena/bots/uncertainty/inference.py",
                "src/sixnimmt/arena/bots/uncertainty/likelihood.py",
            ]
        },
        "versions": {name: importlib.metadata.version(name) for name in ["numpy", "scipy", "emcee"]},
        "options": settings,
        "retained_draws_model": retained.model.model_dump(),
        "repetitions": args.repetitions,
        "cases": [],
        "warm_trajectories": [],
        "baseline_inference_sha256": hashlib.sha256(
            (args.baseline_root / "src/sixnimmt/arena/bots/uncertainty/inference.py").read_bytes()
        ).hexdigest(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for players, hand, play in CASES:
        history, view = trajectories[players][hand, play]
        row = {"players": players, "hand": hand, "play": play, "regular_seconds": regular_times(view), "runs": {}}
        for label, bot_class, model_class, configured in [
            ("before", old_bot, old_model, matched),
            ("matched_budget", SimulationBot, OpponentModel, matched),
            ("retained_draws", SimulationBot, OpponentModel, retained),
        ]:
            if label == "before" and previous is not None:
                saved = next(
                    case
                    for case in previous["cases"]
                    if (case["players"], case["hand"], case["play"]) == (players, hand, play)
                )
                row["runs"][label] = saved["runs"][label]
                continue
            runs = []
            for repeat in range(args.repetitions):
                if label == "before":
                    bot = old_bot(123 + repeat, configured, REGISTRY["closest_gap"].build(123 + repeat))
                else:
                    bot = bot_class(123 + repeat, configured)
                bot.model = TimedModel(model_class(configured.model))
                runs.append(measure(bot, history, view))
            row["runs"][label] = runs
            print(
                json.dumps({
                    "case": [players, hand, play],
                    "variant": label,
                    "median_seconds": statistics.median(run["seconds"] for run in runs),
                }),
                flush=True,
            )
        result["cases"].append(row)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    for players in [3, 5, 10]:
        bot = SimulationBot(123, options)
        bot.model = TimedModel(OpponentModel(options.model))
        decisions = []
        for (hand, play), (history, view) in sorted(trajectories[players].items()):
            if (hand, play) > (3, 5):
                break
            decisions.append({"hand": hand, "play": play, **measure(bot, history, view)})
        result["warm_trajectories"].append({"players": players, "decisions": decisions})
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(
            json.dumps({"warm_players": players, "total_seconds": sum(row["seconds"] for row in decisions)}), flush=True
        )


if __name__ == "__main__":
    main()
