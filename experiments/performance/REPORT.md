# Uncertainty-bot performance

Measured on 2026-09-13 against the implementation at `24abe97`. The optimised
simulation bot is **8.6–33.4 times faster** on the six sampled public game states,
with the original inference and rollout budgets preserved. It still takes roughly
**0.6–2.0 seconds per nontrivial decision**, versus microseconds for simple bots.
This makes small experiments less expensive; it does not make the bot suitable
for baseline-scale arena throughput.

## Equal-budget comparison

Both versions use 64 chains, 5,000 discarded MH steps and one subsequent retained
world per chain, epsilon-logit proposal scale 2.5, the three-policy learned mixture
in the example, 512 rollouts per candidate, and horizon 2. The updated version
samples directly from the prior when there is no behavioural evidence.

Times are medians of three fresh-bot decisions, using sampling seeds 123–125 on
identical public observations. Imports and bot construction are excluded. History
validation, target preparation, inference, rollouts, and action selection are
included. Each regular-bot timing is the median of five batches of 1,000 calls on
the same observation. Timing runs are serial; no test suite runs concurrently.

| Players | Hand / turn | Before | After | Speedup | After: inference |
| --- | --- | ---: | ---: | ---: | ---: |
| 3 | 1 / 1 | 5.089 s | 0.569 s | 8.9× | 0.002 s |
| 3 | 1 / 5 | 8.883 s | 1.035 s | 8.6× | 0.621 s |
| 3 | 1 / 9 | 8.672 s | 0.747 s | 11.6× | 0.621 s |
| 3 | 3 / 5 | 18.202 s | 1.013 s | 18.0× | 0.616 s |
| 5 | 1 / 5 | 14.928 s | 1.380 s | 10.8× | 0.695 s |
| 10 | 3 / 5 | 65.797 s | 1.971 s | 33.4× | 0.775 s |

The ten-player case's inference fell from approximately 64.6 seconds to 0.775
seconds. Rollouts and other decision work still take about 1.2 seconds there;
further inference-only improvements cannot remove that cost. These timings apply
to the example's three ranking policies. The supported `hand_flexibility` policy
uses an exact whole-hand fallback kernel and can be more expensive in a catalogue.

## Remaining cost versus regular bots

| Players, hand / turn | `closest_gap` | `hand_flexibility` | Simulation / closest | Simulation / flexibility |
| --- | ---: | ---: | ---: | ---: |
| 3, 1 / 5 | 14.3 µs | 0.172 ms | 72,550× | 6,027× |
| 5, 1 / 5 | 17.1 µs | 0.174 ms | 80,685× | 7,952× |
| 10, 3 / 5 | 14.8 µs | 0.173 ms | 133,123× | 11,377× |

Across all six states, simulation remains about 73,000–497,000 times slower than
`closest_gap`, and 1,400–24,400 times slower than `hand_flexibility` per selection.
These are **decision-time ratios**, not full-match throughput ratios. Stochastic
card choices can differ because sampling streams change; this is not a win-rate
or strategy-strength evaluation.

## What changed

- Likelihood evaluation is batched across chains and changed opponents. Deterministic
  card rankings are cached from the actual policy implementations. Historical
  agreement counts by policy and hand size replace a loop over every past play on
  every proposal. The whole-hand flexibility policy retains its exact kernel.
- Previous chain endpoints initialise later decisions. Revealed cards are moved to
  their known owners and removed. New deals replace all sampled cards while keeping
  behavioural parameters. Full burn-in and the full posterior are still used;
  this is a warm start, not an unweighted sequential Bayesian update.
- An opening observation with no behavioural evidence draws directly from the
  prior. A sole legal card, or a bait decision whose only candidate is the fallback,
  bypasses inference and rollouts while still recording public history.
- Chain count and draw interval are explicit settings, allowing several retained
  worlds per chain without increasing the number of burn-in chains.

The scalar posterior and original proposal remain available for independent
correctness comparisons and the existing MCMC diagnostic experiments. emcee still
performs MH acceptance; NumPy and SciPy perform the batched numerical work.

## Warm starts and forced choices

Each updated bot was also fed a complete sequence of public observations through
hand 3, turn 5, without resetting its model. These are 25 decisions on a controlled
baseline trajectory, **not 25 decisions from a new self-play match**. All succeeded
without inference recovery.

| Players | Total for 25 decisions | Last decision | Last inference |
| --- | ---: | ---: | ---: |
| 3 | 21.79 s | 1.002 s | 0.604 s |
| 5 | 27.57 s | 1.217 s | 0.639 s |
| 10 | 41.15 s | 1.937 s | 0.734 s |

The sole-card decisions took 29–97 microseconds, including accumulated-history
observation. Warm starts preserve useful state but do not remove the configured
5,000-step warm-up. Most of the measured speedup comes from batching and sufficient
statistics, not from assuming that previous draws have already converged to the
new target.

## Why the example keeps 64 chains

Eight chains, each retaining eight worlds 100 MH steps apart, were also tested.
This gives the same nominal 64 worlds and improves the mid-game decision times by
about 11–14%, but the worlds are correlated. In 12 independent cold-start runs per
case, their epsilon means had appreciably larger error relative to saved long-chain
references:

| Case | 64 chains × 1 draw: epsilon RMSE | 8 chains × 8 draws: epsilon RMSE |
| --- | ---: | ---: |
| 5 players, hand 1 / turn 5 | 0.02289 | 0.03776 |
| 10 players, hand 3 / turn 5 | 0.00552 | 0.00856 |

RMSE pools all opponents and replicate estimates of the posterior epsilon mean.
The increase is approximately 55–65%. The reference is itself a numerical
approximation: the five-player reference had weak tail diagnostics, and neither
these comparisons nor exact target agreement establish convergence in every game.
The ten-player reference comparison has no policy-weight error because the sampled
labels were constant; that is not evidence that all policy modes have been explored.

The example therefore retains **64 chains, one world per chain, and the original
burn-in budget**. Multiple retained draws are available for explicit experiments,
not silently substituted for independent chains. Warm starts also do not justify
a smaller burn-in budget without further mixing diagnostics.

## Reproduction and evidence

Raw timings, selected cards, candidate estimates, warm-start diagnostics, dependency
versions, and source hashes are in [results.json](results.json). Repeated sampling
comparisons are in [sampling.json](sampling.json).

Archive the baseline, then run the benchmark from the repository root:

```bash
mkdir -p /tmp/sixnimmt-before
git archive 24abe97 -o /tmp/sixnimmt-before.tar
tar -xf /tmp/sixnimmt-before.tar -C /tmp/sixnimmt-before
uv run python -m experiments.performance.benchmark \
  --baseline-root /tmp/sixnimmt-before --baseline-revision 24abe97 \
  --output /tmp/sixnimmt-performance.json
```

The benchmark loads the archived inference and simulation code alongside the
current code, using their shared engine and policy implementations. The added
chain-count settings are ignored by the old model. `--reuse-before` can reuse the
saved baseline rows while rerunning all updated variants; the checked-in result
uses baseline measurements from the same session before the final batching change.

The controlled opponents cycle highest-card, lowest-card and closest-gap policies,
with epsilon zero, at match seed 123. Broader noisy-policy, catalogue, and player
count coverage remains useful before generalising these timings or recommending
smaller budgets.

To rerun the sampling comparison, first reproduce the corresponding `.npz`
trajectories using the cases and commands in the
[MCMC calibration report](../mcmc/REPORT.md), then run:

```bash
uv run python -m experiments.performance.sampling \
  --reference-dir /tmp/sixnimmt-mcmc --output /tmp/sixnimmt-sampling.json
```

The comparison discards the first 10,000 saved reference draws (100,000 underlying
MH steps) and uses the rest. Large reference trajectories are not checked in.

## Validation

- `make check` passed: lock consistency, pre-commit hooks, formatting and types.
- Default suite: 720 passed; the 25 `arena_slow` cases were excluded.
- Targeted volume suite: all six uncertainty cases passed, covering 60 matches
  with classic/communication modes, two to ten players, and several horizons.
- The example CLI arena finished its six-hand match with no failed, abandoned, or
  forfeited match. This smoke test was not used as a timing or win-rate benchmark.
- Exact scalar/batched likelihood agreement covers every supported policy, extreme
  epsilon values, cached-history targets and rejected/reordered proposal batches.
  Tiny-model sampling agrees with the analytically enumerated posterior.
- Tests cover card conservation through skipped reveals and new deals, fixed
  mixture weights, prior/forced-choice fast paths, and worker determinism.

The full 8,000-match volume suite was not run.
