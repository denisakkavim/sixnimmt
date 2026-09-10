"""Reproduce MCMC calibration on public observations from controlled arena games."""

import argparse
import copy
import hashlib
import importlib.metadata
import json
import math
import platform
import random
import time
from pathlib import Path
from typing import Any

import emcee
import numpy as np
from arviz_stats.base import array_stats as az
from numpy.typing import NDArray
from scipy.special import expit, logit

from sixnimmt.arena.bots import REGISTRY, ActionBatch, Rejection
from sixnimmt.arena.bots.uncertainty.history import PublicHistory
from sixnimmt.arena.bots.uncertainty.inference import LegalProposal, OpponentModel, Posterior
from sixnimmt.arena.bots.uncertainty.options import OpponentModelOptions, PolicyName
from sixnimmt.arena.bots.uncertainty.policies import choose_card
from sixnimmt.arena.runner import run_match
from sixnimmt.engine.actions import Action, SelectCardAction
from sixnimmt.engine.rules import GameRules
from sixnimmt.engine.views import MatchView

CATALOGUE: list[PolicyName] = ["highest_card", "lowest_card", "closest_gap"]
SNAPSHOTS = [(1, 1), (1, 5), (1, 9), (3, 5)]


class Recorder:
    def __init__(self) -> None:
        self.history = PublicHistory()
        self.snapshots: dict[tuple[int, int], tuple[PublicHistory, MatchView]] = {}
        self.bot = REGISTRY["closest_gap"].build(1)

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action | ActionBatch:
        self.history.observe(view)
        key = (view.hand_number, view.play_number)
        if key in SNAPSHOTS and key not in self.snapshots and "select_card" in view.legal_actions:
            self.snapshots[key] = (copy.deepcopy(self.history), view)
        return self.bot.act(view, rejection)


class ControlledOpponent:
    def __init__(self, policy: PolicyName, epsilon: float, seed: int) -> None:
        self.policy = policy
        self.epsilon = epsilon
        self.rng = random.Random(seed)  # noqa: S311 -- reproducible experiment
        self.base = REGISTRY["closest_gap"].build(seed)

    def act(self, view: MatchView, rejection: Rejection | None = None) -> Action | ActionBatch:
        if "select_card" in view.legal_actions and view.you.selection is None:
            return SelectCardAction(card=choose_card(self.policy, self.epsilon, view.rows, view.you.hand, self.rng))
        return self.base.act(view, rejection)


def capture(players: int, noise: float, seed: int) -> dict[tuple[int, int], tuple[PublicHistory, MatchView]]:
    recorder = Recorder()
    bots = [recorder, *[ControlledOpponent(CATALOGUE[i % 3], noise, seed + i + 1) for i in range(players - 1)]]
    run_match(bots, seed, rules=GameRules(target_score=10000), max_actions=players * 10 * 3 + 20)
    return recorder.snapshots


def observable_traces(chain: NDArray[np.float64], posterior: Posterior) -> tuple[NDArray[np.float64], list[str]]:
    # Axes are (draw, chain, feature). Card slots are exchangeable: diagnose
    # allocation summaries and ownership indicators, not arbitrary slot order.
    boundary = len(posterior.unseen)
    opponents = len(posterior.opponent_ids)
    values = []
    names = []
    for index in range(opponents):
        values.append(expit(chain[:, :, boundary + opponents + index]))
        names.append(f"epsilon_{index}")
        if posterior.options.mode == "learned_mixture":
            for policy in range(len(posterior.options.policies)):
                values.append((chain[:, :, boundary + index] == policy).astype(float))
                names.append(f"policy_{index}_{policy}")
        start = sum(posterior.sizes[:index])
        hand = chain[:, :, start : start + posterior.sizes[index]]
        for label, function in [("min", np.min), ("max", np.max), ("mean", np.mean)]:
            values.append(function(hand, axis=2))
            names.append(f"hand_{index}_{label}")
        for card in np.quantile(posterior.unseen, [0.2, 0.5, 0.8], method="nearest"):
            values.append(np.any(hand == card, axis=2).astype(float))
            names.append(f"owns_{index}_{int(card)}")
    return np.stack(values, axis=2).transpose(2, 1, 0), names


def diagnose(values: NDArray[np.float64], names: list[str]) -> dict[str, Any]:
    variable = np.ptp(values, axis=(1, 2)) > 0
    constants = [name for name, varying in zip(names, variable, strict=True) if not varying]
    data = values[variable]
    rhat = np.asarray(az.rhat(data, chain_axis=1, draw_axis=2))
    bulk = np.asarray(az.ess(data, method="bulk", chain_axis=1, draw_axis=2))
    tail = np.asarray(az.ess(data, method="tail", prob=0.05, chain_axis=1, draw_axis=2))
    tested = [name for name, varying in zip(names, variable, strict=True) if varying]
    return {
        "rhat_max": float(np.max(rhat)),
        "bulk_ess_min": float(np.min(bulk)),
        "tail_ess_min": float(np.min(tail)),
        "worst_rhat": tested[int(np.argmax(rhat))],
        "constants": constants,
        "nonfinite_diagnostics": int(np.sum(~np.isfinite(rhat))),
        "rhat": dict(zip(tested, rhat.tolist(), strict=True)),
        "bulk_ess": dict(zip(tested, bulk.tolist(), strict=True)),
    }


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def run_case(args: argparse.Namespace) -> None:
    history, view = capture(args.players, args.noise, args.seed)[(args.hand, args.play)]
    policies: list[PolicyName] = (
        CATALOGUE
        if args.catalogue == "three"
        else [
            "random",
            "lowest_card",
            "highest_card",
            "lowest_fitting_card",
            "highest_fitting_card",
            "closest_gap",
            "coldest_row",
            "hand_flexibility",
        ]
    )
    if args.mode == "single_policy":
        policies = ["highest_card"]
    options = OpponentModelOptions(
        policies=policies, mode=args.mode, particle_count=4, burn_in_steps=1, epsilon_proposal_scale=args.scale
    )
    posterior = OpponentModel(options)._posterior(history, view)
    rng = np.random.RandomState(args.seed + 100)
    count = len(posterior.opponent_ids)
    positions = []
    for index in range(4):
        labels = (
            np.arange(count) % len(policies) if args.mode == "fixed_mixture" else rng.randint(len(policies), size=count)
        )
        positions.append(
            np.r_[rng.permutation(posterior.unseen), labels, np.full(count, logit([0.01, 0.25, 0.75, 0.99][index]))]
        )
    coordinates = np.asarray(positions)
    sampler = emcee.EnsembleSampler(
        4,
        coordinates.shape[1],
        posterior,
        moves=emcee.moves.MHMove(LegalProposal(len(posterior.unseen), count, options)),
    )
    sampler.random_state = rng.get_state()
    started = time.perf_counter()
    sampler.run_mcmc(coordinates, args.steps, skip_initial_state_check=True)
    elapsed = time.perf_counter() - started
    chain = sampler.get_chain()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.output.with_suffix(".npz"), chain=chain[::10], view=view.model_dump_json())
    print(f"Sampled {args.output.name}: {elapsed:.2f}s", flush=True)
    windows = []
    for burn in [200, 1000, 5000, 10000, 20000, 40000, 100000, 200000]:
        if burn + args.window > args.steps:
            continue
        values, names = observable_traces(chain[burn : burn + args.window : 10], posterior)
        result = diagnose(values, names)
        result.update({
            "burn_in_steps": burn,
            "diagnostic_steps": args.window,
            "epsilon_means": np.mean(expit(chain[burn : burn + args.window, :, -count:]), axis=(0, 1)).tolist(),
        })
        windows.append(result)
    payload = {
        "case": vars(args) | {"output": str(args.output)},
        "elapsed_seconds": elapsed,
        "acceptance": sampler.acceptance_fraction.tolist(),
        "windows": windows,
        "versions": {
            name: importlib.metadata.version(name) for name in ["sixnimmt", "emcee", "numpy", "scipy", "arviz-stats"]
        },
        "platform": platform.platform(),
        "inference_sha256": hashlib.sha256(
            Path("src/sixnimmt/arena/bots/uncertainty/inference.py").read_bytes()
        ).hexdigest(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(json_safe(payload), indent=2, allow_nan=False) + "\n")
    print(
        json.dumps({
            "output": str(args.output),
            "seconds": round(elapsed, 2),
            "windows": [
                {key: row[key] for key in ["burn_in_steps", "rhat_max", "bulk_ess_min", "tail_ess_min", "worst_rhat"]}
                for row in windows
            ],
        }),
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--players", type=int, required=True)
    parser.add_argument("--hand", type=int, required=True)
    parser.add_argument("--play", type=int, required=True)
    parser.add_argument("--noise", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=123)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument(
        "--mode", choices=["single_policy", "fixed_mixture", "learned_mixture"], default="learned_mixture"
    )
    parser.add_argument("--catalogue", choices=["three", "eight"], default="three")
    parser.add_argument("--steps", type=int, default=20000)
    parser.add_argument("--window", type=int, default=10000)
    parser.add_argument("--output", type=Path, required=True)
    run_case(parser.parse_args())


if __name__ == "__main__":
    main()
