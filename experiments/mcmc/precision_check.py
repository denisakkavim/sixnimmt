"""Nested bootstrap of finite world and rollout budgets against a posterior cost bank."""

import argparse
import json
from pathlib import Path

import numpy as np

from experiments.mcmc.diagnose import capture
from sixnimmt.arena.bots.uncertainty.inference import OpponentModel
from sixnimmt.arena.bots.uncertainty.options import SimulationOptions
from sixnimmt.arena.bots.uncertainty.simulation import rollout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    case = json.loads(args.reference.read_text())["case"]
    if case["mode"] != "learned_mixture" or case["catalogue"] != "three":
        msg = "This comparison supports three-policy learned-mixture references only"
        raise ValueError(msg)
    history, view = capture(case["players"], case["noise"], case["seed"])[(case["hand"], case["play"])]
    options = SimulationOptions.model_validate({
        "model": {
            "policies": ["highest_card", "lowest_card", "closest_gap"],
            "mode": case["mode"],
            "particle_count": 1,
            "burn_in_steps": 1,
            "epsilon_proposal_scale": case["scale"],
        },
        "sample_count": 1,
        "horizon": 3,
        "continuation_policy": "closest_gap",
        "row_policy": {"policy": "cheapest"},
        "objective": {"kind": "mean"},
        "cutoff_evaluation": "zero",
        "fallback_strategy": "closest_gap",
    })
    posterior = OpponentModel(options.model)._posterior(history, view)
    chain = np.load(args.reference.with_suffix(".npz"))["chain"][10000:]
    pool = chain.reshape(-1, chain.shape[-1])
    rng = np.random.default_rng(991)
    coordinates = pool[rng.choice(len(pool), size=1024, replace=False)]
    candidates = sorted(view.you.hand)
    costs = np.empty((len(coordinates), 8, len(candidates)))
    for i, row in enumerate(coordinates):
        world = posterior.world(row)
        for trial in range(8):
            for column, card in enumerate(candidates):
                costs[i, trial, column] = rollout(view, world, card, options, 8000 + i * 8 + trial)
    np.savez_compressed(args.output.with_suffix(".npz"), costs=costs)
    reference_mean = np.mean(costs, axis=(0, 1))
    best = int(np.argmin(reference_mean))
    output = {
        "case": case,
        "reference_best": candidates[best],
        "candidates": candidates,
        "reference_mean": reference_mean.tolist(),
        "horizon": 3,
        "repetitions": 1000,
        "budgets": [],
    }
    for particles in [32, 64, 128, 256]:
        for samples in [32, 128, 512, 2048]:
            regrets = []
            squared_errors = []
            for _ in range(1000):
                bag = rng.integers(len(costs), size=particles)
                worlds = bag[rng.integers(particles, size=samples)]
                trials = rng.integers(8, size=samples)
                estimate = np.mean(costs[worlds, trials], axis=0)
                choice = int(np.argmin(estimate))
                regrets.append(reference_mean[choice] - reference_mean[best])
                squared_errors.append(np.mean((estimate - reference_mean) ** 2))
            output["budgets"].append({
                "particle_count": particles,
                "sample_count": samples,
                "zero_reference_regret_fraction": float(np.mean(np.array(regrets) == 0)),
                "mean_reference_regret": float(np.mean(regrets)),
                "cost_rmse": float(np.sqrt(np.mean(squared_errors))),
            })
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output), flush=True)


if __name__ == "__main__":
    main()
