"""Compare actual prior-initialised terminal samples with a long-chain reference."""

import argparse
import json
import time
from pathlib import Path

import emcee
import numpy as np
from numpy.typing import NDArray
from scipy.special import logit

from experiments.mcmc.diagnose import capture, json_safe, observable_traces
from sixnimmt.arena.bots.uncertainty.inference import LegalProposal, OpponentModel, Posterior
from sixnimmt.arena.bots.uncertainty.options import (
    OpponentModelOptions,
    PenaltyObjective,
    PolicyName,
    RowPolicyOptions,
    SimulationOptions,
)
from sixnimmt.arena.bots.uncertainty.simulation import rollout
from sixnimmt.engine.views import MatchView


def candidate_costs(
    coordinates: NDArray[np.float64], posterior: Posterior, view: MatchView, horizon: int
) -> NDArray[np.float64]:
    options = SimulationOptions(
        model=posterior.options,
        sample_count=1,
        horizon=horizon,
        continuation_policy="closest_gap",
        row_policy=RowPolicyOptions(policy="cheapest"),
        objective=PenaltyObjective(kind="mean"),
        cutoff_evaluation="zero",
        fallback_strategy="closest_gap",
    )
    candidates = sorted(view.you.hand)
    costs = np.empty((len(coordinates), len(candidates)))
    for index, row in enumerate(coordinates):
        world = posterior.world(row)
        for column, candidate in enumerate(candidates):
            costs[index, column] = (
                sum(rollout(view, world, candidate, options, 8000 + index * 8 + trial) for trial in range(8)) / 8
            )
    return costs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--particles", type=int, default=64)
    parser.add_argument("--max-burn", type=int, default=20000)
    parser.add_argument("--scale", type=float)
    parser.add_argument("--sampling-seed", type=int, default=321)
    args = parser.parse_args()
    reference = json.loads(args.reference.read_text())
    case = reference["case"]
    if case["mode"] != "learned_mixture" or case["catalogue"] != "three":
        msg = "This comparison supports three-policy learned-mixture references only"
        raise ValueError(msg)
    history, view = capture(case["players"], case["noise"], case["seed"])[(case["hand"], case["play"])]
    policies: list[PolicyName] = ["highest_card", "lowest_card", "closest_gap"]
    options = OpponentModelOptions(
        policies=policies,
        mode=case["mode"],
        particle_count=args.particles,
        burn_in_steps=1,
        epsilon_proposal_scale=case["scale"] if args.scale is None else args.scale,
    )
    posterior = OpponentModel(options)._posterior(history, view)
    rng = np.random.RandomState(args.sampling_seed + case["seed"])
    count = len(posterior.opponent_ids)
    coordinates = np.array([
        np.r_[rng.permutation(posterior.unseen), rng.randint(len(policies), size=count), logit(rng.uniform(size=count))]
        for _ in range(args.particles)
    ])
    sampler = emcee.EnsembleSampler(
        args.particles,
        coordinates.shape[1],
        posterior,
        moves=emcee.moves.MHMove(LegalProposal(len(posterior.unseen), count, options)),
    )
    sampler.random_state = rng.get_state()
    ref_chain = np.load(args.reference.with_suffix(".npz"))["chain"][10000:]
    reference_values, names = observable_traces(ref_chain, posterior)
    reference_mean = np.mean(reference_values, axis=(1, 2))
    ref_pool = ref_chain.reshape(-1, ref_chain.shape[-1])
    ref_coordinates = ref_pool[rng.choice(len(ref_pool), size=1024, replace=False)]
    reference_costs = {h: candidate_costs(ref_coordinates, posterior, view, h) for h in [1, 3]}
    output = {
        "case": case,
        "particles": args.particles,
        "epsilon_proposal_scale": options.epsilon_proposal_scale,
        "sampling_seed": args.sampling_seed,
        "reference_means": dict(zip(names, reference_mean.tolist(), strict=True)),
        "reference_costs": {h: np.mean(c, axis=0).tolist() for h, c in reference_costs.items()},
        "candidates": sorted(view.you.hand),
        "budgets": [],
    }
    previous = 0
    state = coordinates
    for burn in [200, 1000, 5000, 10000, 20000, 40000]:
        if burn > args.max_burn:
            continue
        started = time.perf_counter()
        state = sampler.run_mcmc(state, burn + 1 - previous, skip_initial_state_check=True, store=False)
        seconds = time.perf_counter() - started
        previous = burn + 1
        values, _ = observable_traces(state.coords[None, :, :], posterior)
        means = np.mean(values, axis=(1, 2))
        eps_indices = [i for i, name in enumerate(names) if name.startswith("epsilon")]
        row = {
            "burn_in_steps": burn,
            "incremental_seconds": seconds,
            "epsilon_max_abs_error": float(np.max(np.abs(means[eps_indices] - reference_mean[eps_indices]))),
            "means": dict(zip(names, means.tolist(), strict=True)),
            "standard_errors": dict(
                zip(names, (np.std(values[:, :, 0], axis=1, ddof=1) / np.sqrt(args.particles)).tolist(), strict=True)
            ),
            "decisions": {},
        }
        for horizon in [1, 3]:
            costs = candidate_costs(state.coords, posterior, view, horizon)
            avg = np.mean(costs, axis=0)
            ref_avg = np.mean(reference_costs[horizon], axis=0)
            best = int(np.argmin(avg))
            row["decisions"][horizon] = {
                "chosen": output["candidates"][best],
                "reference_chosen": output["candidates"][int(np.argmin(ref_avg))],
                "reference_regret": float(ref_avg[best] - np.min(ref_avg)),
                "cost_rmse": float(np.sqrt(np.mean((avg - ref_avg) ** 2))),
                "costs": avg.tolist(),
                "particle_half_choices": [
                    output["candidates"][int(np.argmin(np.mean(half, axis=0)))] for half in np.array_split(costs, 2)
                ],
            }
        output["budgets"].append(row)
        args.output.write_text(json.dumps(json_safe(output), indent=2, allow_nan=False) + "\n")
        np.savez_compressed(args.output.with_name(args.output.stem + f"-{burn}.npz"), coordinates=state.coords)
        print(
            json.dumps({
                "case": case["players"],
                "burn": burn,
                "seconds": round(seconds, 1),
                "epsilon_error": row["epsilon_max_abs_error"],
                "decisions": row["decisions"],
            }),
            flush=True,
        )


if __name__ == "__main__":
    main()
