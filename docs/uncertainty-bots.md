# Probabilistic bots

The arena registers two bots using the [probabilistic gameplay model](uncertainty-model.md):

- `simulation` evaluates every card using sampled joint hidden hands and opponent
  behaviour. Horizon 1 implements penalty-distribution evaluation; longer horizons
  add policy rollouts.
- `model_based_bait` evaluates cards targeting currently full rows, together with
  the configured fallback card. It attempts bait only for a strictly lower
  estimated mean penalty. Equal cost keeps the fallback.

Both learn from public card selections across all hands of the current match.
They start with a uniform legal-deal prior, equal policy probabilities, and an
independent continuous `Beta(1,1)` mistake probability for each opponent.
They receive ordinary player views, without actual opponents' hands, unrevealed
selections, the match seed, or the actual undealt cards.

## Running an experiment

Use the complete [example player file](../examples/arena-uncertainty-players.json):

```bash
uv run sixnimmt arena --players-file examples/arena-uncertainty-players.json --games 1 --seed 123
```

The example uses the exploratory settings from our
[MCMC calibration report](../experiments/mcmc/REPORT.md): 5,000 burn-in proposals,
64 retained worlds, 512 rollouts per candidate, and epsilon proposal scale 2.5.
The explicit `chain_count` and `draw_interval` settings separate chain count from
retained world count; see the [performance comparison](../experiments/performance/REPORT.md).
These are starting settings for small-table experiments, not defaults or a
convergence guarantee. Ten-player stress cases failed diagnostics even with much
longer runs. Check stability before drawing conclusions about strategy strength.

The simulation player's options are:

```json
{
  "model": {
    "policies": ["lowest_card", "highest_card", "closest_gap"],
    "mode": "learned_mixture",
    "particle_count": 64,
    "chain_count": 64,
    "draw_interval": 1,
    "burn_in_steps": 5000,
    "epsilon_proposal_scale": 2.5
  },
  "sample_count": 512,
  "horizon": 2,
  "continuation_policy": "closest_gap",
  "row_policy": {"policy": "cheapest"},
  "objective": {"kind": "mean"},
  "cutoff_evaluation": "zero"
}
```

All fields above are required. Older configurations must add `chain_count` and
`draw_interval`; setting them to `particle_count` and `1` respectively preserves
the old number of chains and retained draws. Lookahead has no recovery strategy;
remove `fallback_strategy` and `fallback_options` from older simulation configurations.
Unsupported policies, unknown keys, and invalid or missing parameters are rejected.

## Opponent model

| Setting | Meaning |
| --- | --- |
| `policies` | Nonempty list of distinct supported policies |
| `mode` | `single_policy`, `fixed_mixture`, or `learned_mixture` |
| `particle_count` | Total retained worlds; must be a positive multiple of `chain_count` |
| `chain_count` | Positive number of separately evolving MH chains |
| `draw_interval` | Positive MH steps between retained draws, including the first draw after burn-in |
| `burn_in_steps` | Positive discarded warm-up proposals per chain, on every inference with behavioural evidence |
| `epsilon_proposal_scale` | Positive finite standard deviation of epsilon-logit proposals |

The supported catalogue is `random`, `lowest_card`, `highest_card`,
`lowest_fitting_card`, `highest_fitting_card`, `closest_gap`, `coldest_row`,
and `hand_flexibility`. These policies have exact card-selection probabilities
using only the hypothetical player's own hand and the public board. Parameterised,
history-dependent, LLM, and nested simulation policies are not yet catalogue members.

`single_policy` requires exactly one policy, assumed for every opponent.
`fixed_mixture` samples equal-prior assignments and holds each chain's assignment
fixed while learning its conditional hidden hands and epsilon. `learned_mixture`
also updates policy assignments. Finite empirical policy frequencies need not be
exactly equal, even when their underlying prior is equal.

Inference reconstructs opponents' past possible hands from their sampled remaining
cards and public reveals. Completed hands reveal all ten original cards, so their
behavioural likelihoods can be evaluated directly. The full accumulated likelihood
is used once; repeated observations, commit callbacks, and row choices do not count
a selection again. Card knowledge resets at each deal; behavioural evidence persists.

Observed row choices update reconstructed boards but do not contribute behavioural
likelihoods. Predicted opponents take the cheapest row, breaking ties by row index.
Messages and selection timing are not modelled.

### Inference and library choice

The implementation uses **emcee** for Metropolis–Hastings acceptance and chain
execution, **NumPy** for arrays and private random streams, and **SciPy** for stable
logit/logistic calculations. Custom code defines the game likelihood and symmetric
proposals: swap two unknown-card slots, change a policy label, or move an epsilon
logit. Proposed deals preserve hand sizes, card uniqueness, reveals, and the undealt
remainder. The transformed target includes the Beta prior's Jacobian.

PyMC and `particles` were evaluated on a small hidden-hand example with an
analytically known posterior. Both represented that posterior successfully.
PyMC needed graph setup for the black-box policy likelihood; `particles`'s standard
sampling path uses global NumPy randomness. emcee's [custom MH interface](https://emcee.readthedocs.io/en/stable/user/moves/)
accepts legal-deal proposals directly and provides a private sampler RNG, fitting
concurrent arena execution without a global RNG lock. See also [PyMC compound sampling](https://www.pymc.io/projects/examples/en/latest/samplers/sampling_compound_step.html)
and [particles SMC samplers](https://particles-sequential-monte-carlo-in-python.readthedocs.io/en/latest/notebooks/SMC_samplers_tutorial.html).

The likelihood is batched across chains and opponents. For each supported
card-ranking policy, inference caches which alternative cards would outrank an
observed selection on its original board. Completed-hand evidence is compressed
into agreement counts by policy and hand size, so each proposal's likelihood work
does not grow with the number of completed hands. `hand_flexibility` depends on the
whole hand; it uses the original exact policy kernel with a per-inference hand cache.
Only changed opponent components are reevaluated. The scalar target remains as a
reference for correctness tests and diagnostic experiments.

With no behavioural observations, the legal-deal prior is the posterior and worlds
are drawn directly, without MCMC. Otherwise, the model reuses previous chain endpoints
as warm starts. Within a hand, newly revealed cards are swapped into their observed
owners' hands and removed; a new hand draws fresh legal cards while retaining
behavioural parameters. This repair is **not** an exact posterior update. Every
inference still runs the full configured burn-in against the full accumulated
likelihood and original priors. Earlier evidence is neither lost nor counted twice.
No stale hand is used as a rollout world.

After warm-up each chain contributes `particle_count / chain_count` equally weighted
worlds, separated by `draw_interval` MH steps. Fixed-mixture mode preserves each
chain's policy assignment and equal weight. More draws from fewer chains can reduce
work, but the draws are correlated; `particle_count` is not effective sample size,
and spacing draws does not guarantee independence. Use `chain_count = particle_count`
and `draw_interval = 1` to retain one world from each chain.

An explicit burn-in phase does not establish convergence. Small budgets can leave
substantial initialisation bias; warm starts also need mixing after a surprising
reveal. The bot does not compute MCMC effective sample size from retained worlds.
Epsilon intervals are empirical posterior approximations. The
[calibration report](../experiments/mcmc/REPORT.md) describes remaining slow-mixing
cases; the [performance report](../experiments/performance/REPORT.md) distinguishes
speed improvements from sampling-quality changes.

## Evaluation and continuation

| Setting | Meaning |
| --- | --- |
| `sample_count` | Positive number of rollouts per candidate |
| `horizon` | Positive total turns including this turn, or `remaining_hand` |
| `continuation_policy` | Our future card policy, from the same eight-policy catalogue |
| `row_policy` | Our actual and simulated row-choice rule |
| `objective` | Function of accumulated bull-head penalties |
| `cutoff_evaluation` | Must explicitly be `zero`; no estimate beyond the horizon |

The horizon stops at the current hand boundary; no new hands are dealt. Each rollout
keeps the same sampled opponent hands, policies, and epsilon values, removing played
cards and drawing fresh action randomness each turn. Our continuation follows its
configured policy; it does not optimise future sequences or relearn inside rollouts.

Candidates reuse the same sampled worlds and initial random streams. Placement,
captures, and scoring use the existing rules engine. Objective ties choose the lower
card; bait ties retain the fallback before breaking ties among improving candidates.

Our row rule is either `{"policy": "cheapest"}` or
`{"policy": "hand_aware", "max_extra_penalty": 2}`. The latter requires an explicit
non-negative allowance and uses the existing hand-aware strategy consistently in
real decisions and simulated continuations.

| Objective | Configuration |
| --- | --- |
| Mean penalty | `{"kind": "mean"}` |
| Probability of any pickup | `{"kind": "pickup_probability"}` |
| Probability of exceeding N bull heads | `{"kind": "threshold_exceedance", "threshold": 5}`; non-negative threshold |
| Mean of worst fraction | `{"kind": "upper_tail", "tail_fraction": 0.2}`; fraction in (0, 1] |

Upper-tail evaluation includes only the required fraction of a boundary sample.
All objectives use accumulated penalties over the horizon, excluding prior points.

`model_based_bait` requires horizon 1, mean penalty, and cheapest row choice. Other
settings remain explicit, including the unused continuation policy. Bait additionally
requires `fallback_strategy`: the registered reference strategy to follow unless
bait has a strictly better estimated cost. `fallback_options` configures that
strategy and is empty when omitted. Nested settings are validated before workers
start. These settings belong only to bait, not to lookahead.

If the reference strategy returns a batch or anything other than one legal card
proposal, bait passes that proposal through. It never drops batch memory/messages or
calls the fallback twice to obtain the same proposal. When the fallback is the only
candidate, bait returns it without inference or rollouts. Both bots also return a
sole legal card immediately. Public history is still accumulated on these turns.

## Diagnostics and limits

Bot statistics include inference method and budgets, observed-play count, empirical
policy weights per opponent, epsilon means and central 90% intervals, latest candidate
values. Chain count, draw interval, and whether a warm
start was used are reported. `evaluation` identifies simulation, a sole card or
candidate, or a bait reference proposal. Model diagnostics describe the last inference;
skipped decisions clear candidate values but retain those earlier model diagnostics. The normal bot-statistics trace mechanism
records these values.

Missing or inconsistent history and other inference errors propagate to the arena
and fail the match. Neither bot switches strategies to conceal an inference error.
The bot needs to be present from the start of a hand. A new instance is
created per match; there is no cross-match identity tracking.

Fixed seeds reproduce decisions across thread and process workers in the same
software environment. Dependency upgrades can change numerical results; retain the
lockfile with experiment provenance.

Timing-aware urgency selection, nonzero cutoff evaluators, and richer learned
row-choice or history-dependent policies remain unimplemented. Their behavioural
choices require further specification.
