"""Measure finite-sample error against saved long-chain references, not win rate."""

import argparse
import json
import random
from pathlib import Path

import numpy as np
from scipy.special import expit

from experiments.mcmc.diagnose import CATALOGUE
from experiments.performance.benchmark import capture
from sixnimmt.arena.bots.uncertainty.inference import OpponentModel
from sixnimmt.arena.bots.uncertainty.options import OpponentModelOptions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=12)
    args = parser.parse_args()
    output = []
    for players, hand, play in [(5, 1, 5), (10, 3, 5)]:
        reference_path = args.reference_dir / f"reference-p{players}-h{hand}-t{play}.npz"
        chain = np.load(reference_path)["chain"][10000:]
        opponent_count = players - 1
        reference_epsilons = np.mean(expit(chain[:, :, -opponent_count:]), axis=(0, 1))
        labels = chain[:, :, -2 * opponent_count : -opponent_count].astype(int)
        reference_policies = np.stack([np.mean(labels == policy, axis=(0, 1)) for policy in range(3)], axis=1)
        history, view = capture(players)[hand, play]
        row = {
            "players": players,
            "hand": hand,
            "play": play,
            "reference_epsilon": reference_epsilons.tolist(),
            "reference_policies": reference_policies.tolist(),
            "variants": {},
        }
        for chains, interval in [(64, 1), (8, 100)]:
            options = OpponentModelOptions(
                policies=CATALOGUE,
                mode="learned_mixture",
                particle_count=64,
                chain_count=chains,
                draw_interval=interval,
                burn_in_steps=5000,
                epsilon_proposal_scale=2.5,
            )
            epsilon_means, policy_means = [], []
            for repeat in range(args.repetitions):
                worlds = OpponentModel(options).infer(history, view, random.Random(1000 + repeat))  # noqa: S311 -- reproducible experiment
                epsilon_means.append(np.mean([world.epsilons for world in worlds], axis=0))
                assignments = np.array([world.policies for world in worlds])
                policy_means.append(np.stack([np.mean(assignments == policy, axis=0) for policy in range(3)], axis=1))
            eps = np.array(epsilon_means)
            weights = np.array(policy_means)
            row["variants"][str(chains)] = {
                "epsilon_rmse": float(np.sqrt(np.mean((eps - reference_epsilons) ** 2))),
                "epsilon_max_mean_error": float(np.max(np.abs(np.mean(eps, axis=0) - reference_epsilons))),
                "policy_rmse": float(np.sqrt(np.mean((weights - reference_policies) ** 2))),
                "epsilon_means": eps.tolist(),
                "policy_means": weights.tolist(),
            }
            print(
                json.dumps({
                    "players": players,
                    "chains": chains,
                    **{
                        k: v
                        for k, v in row["variants"][str(chains)].items()
                        if k.endswith("rmse") or k.endswith("error")
                    },
                }),
                flush=True,
            )
        output.append(row)
        args.output.write_text(json.dumps(output, indent=2) + "\n")


if __name__ == "__main__":
    main()
