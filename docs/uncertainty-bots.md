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

These settings illustrate the configuration and are not calibrated budgets or
defaults. Check posterior mixing and decision stability before drawing conclusions
about strategy strength.

The simulation player's options are:

```json
{
  "model": {
    "policies": ["lowest_card", "highest_card", "closest_gap"],
    "mode": "learned_mixture",
    "particle_count": 32,
    "burn_in_steps": 200,
    "epsilon_proposal_scale": 1.0
  },
  "sample_count": 32,
  "horizon": 2,
  "continuation_policy": "closest_gap",
  "row_policy": {"policy": "cheapest"},
  "objective": {"kind": "mean"},
  "cutoff_evaluation": "zero",
  "fallback_strategy": "closest_gap"
}
```

All fields above are required. `fallback_options` is optional and empty when
omitted; it supplies options for the explicitly named fallback. Unsupported
policies, unknown keys, and invalid or missing parameters are rejected. Nested
fallback settings are validated before arena workers start.

## Opponent model

| Setting | Meaning |
| --- | --- |
| `policies` | Nonempty list of distinct supported policies |
| `mode` | `single_policy`, `fixed_mixture`, or `learned_mixture` |
| `particle_count` | Positive number of independently initialised chains; retain one world per chain |
| `burn_in_steps` | Positive number of discarded warm-up proposals per chain before one retained draw |
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

This is bounded MCMC, not the weighted sequential filter outlined as another option
in the model document. Each decision starts fresh chains and evaluates the entire
history against the original priors. It does not treat the preceding finite sample
as an exact prior or forget earlier hands. All burn-in states are discarded. One additional Metropolis–Hastings step supplies
the retained draw per chain; each retained sample has equal rollout weight. Fixed-mixture mode deliberately retains fixed assignment weights.

An explicit burn-in phase does not establish convergence. Small budgets can leave
substantial initialisation bias. More particles do not
substitute for sufficient movement of each chain. The implementation does not claim
convergence or compute MCMC effective sample size from these terminal samples.
Epsilon intervals are empirical posterior approximations.

## Evaluation and continuation

| Setting | Meaning |
| --- | --- |
| `sample_count` | Positive number of rollouts per candidate |
| `horizon` | Positive total turns including this turn, or `remaining_hand` |
| `continuation_policy` | Our future card policy, from the same eight-policy catalogue |
| `row_policy` | Our actual and simulated row-choice rule |
| `objective` | Function of accumulated bull-head penalties |
| `cutoff_evaluation` | Must explicitly be `zero`; no estimate beyond the horizon |
| `fallback_strategy` | Registered bot used for inference recovery; also the reference play for bait |
| `fallback_options` | Options for that fallback; empty when omitted |

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
settings remain explicit, including the unused continuation policy. If its fallback
returns a batch or anything other than one legal card proposal, it passes that
proposal through and records recovery. It never drops batch memory/messages or
calls the fallback twice to obtain the same proposal.

## Diagnostics and limits

Bot statistics include inference method and budgets, observed-play count, empirical
policy weights per opponent, epsilon means and central 90% intervals, latest candidate
values, and recovery counts/reasons. The normal bot-statistics trace mechanism
records these values.

Missing or inconsistent history triggers the configured fallback and a diagnostic.
The bot normally needs to be present from the start of a hand. A new instance is
created per match; there is no cross-match identity tracking.

Fixed seeds reproduce decisions across thread and process workers in the same
software environment. Dependency upgrades can change numerical results; retain the
lockfile with experiment provenance.

Timing-aware urgency selection, nonzero cutoff evaluators, and richer learned
row-choice or history-dependent policies remain unimplemented. Their behavioural
choices require further specification.
