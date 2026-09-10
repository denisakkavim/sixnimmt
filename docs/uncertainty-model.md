# A probabilistic model of 6 nimmt!

## Status and scope

This document defines the proposed model for uncertainty-aware bots. It is a
design, not a description of an implemented evaluator. The existing engine
continues to own the rules and state transitions.

The model learns opponents' card-selection behaviour from observed play while
accounting for their hidden hands. It begins with uniformly distributed deals,
equally likely candidate policies, and uncertainty about each opponent's
random-choice probability. It predicts distributions of outcomes rather than
assuming a single hidden deal or identifying one policy with certainty.

One simulation evaluator supports a single simultaneous turn or longer rollouts,
with a configurable horizon and explicit continuation policy. Opponent row
choices initially use the cheapest-row rule, breaking ties by row index. Our
row-choice rule comes from the configured continuation policy and is used in
actual and simulated play. Learning opponent row-choice behaviour is a separate
extension.

## 1. State, information, and notation

Let the deck be `D = {1, ..., 104}`, the player count be `n`, and our seat be `i`.
There are four rows, each holding at most five cards. Every player receives ten
cards at each deal; `100 - 10n` cards remain undealt.

| Symbol | Meaning |
| --- | --- |
| `d` | Hand/deal number within the match |
| `t` | Simultaneous play number within the hand |
| `R_t` | Public board immediately before card selection |
| `H_{j,t}` | Player j's remaining hand before selection |
| `U_d` | Undealt cards in deal d |
| `a_{j,t}` | Card revealed by player j in play t |
| `I_t` | Information available to us before selection, including our hand and remembered observations |
| `v_{j,t}` | The corresponding observation available to simulated player j |
| `z_j` | Opponent j's latent card-selection policy |
| `epsilon_j` | Opponent j's latent random-choice probability |
| `theta` | Joint collection of `(z_j, epsilon_j)` for all opponents |
| `b_t` | Our posterior belief over theta and hidden hands |

Hidden hands are disjoint. No card can belong to two players or also be undealt.
Visible cards, captured cards, and remaining hands must jointly satisfy deck
conservation. A sampled world is a complete, consistent allocation, not a set of
independent guesses about individual cards.

We may use our own cards and the public history of reveals, placements, row
choices, and captures. We must not access opponents' actual hands, unrevealed
selections, private messages, the actual undealt remainder, or the engine's seed.
The model's sampling seed is independent of the hidden deal and is used only
for reproducibility.

For the initial version, the policy catalogue contains non-messaging card
policies. Message content, selection timing, and intermediate commitment changes
are not used as behavioural evidence. Communication-mode play is modelled at the
level of the final revealed cards. This does not claim to model negotiation or
selection changes influenced by messages.

## 2. Uniform dealing is the initial prior

At the start of a hand, condition on our ten cards and the four public row-start
cards. The remaining `N = 90` unknown cards are allocated uniformly among the
opponents' ten-card hands and the undealt remainder.

For a valid allocation of unordered hands:

```text
P(H_{-i,1}, U_d | our initial hand, initial rows)
    = 1 / [N! / ((10!)^(n-1) × (100 - 10n)!)]
```

One way to sample this prior is to shuffle those unknown cards, assign ten to
each opponent in seat order, and leave the rest undealt. This respects both
uniformity and card uniqueness. The first policy catalogue should be invariant
to the original order of cards within a hand. An order-sensitive policy would
also require modelling hidden deal order.

Under a uniform allocation of `N_t` unknown cards, an opponent with `h_j` cards
has marginal probability `h_j / N_t` of holding a particular unknown card.
These marginal probabilities are not independent across cards or players.

**Uniform initial dealing does not imply uniform remaining hands after play.**
Once we model selection behaviour, a revealed choice supplies evidence about
which alternatives that player may have retained. The posterior is allowed to
depart from uniformity.

## 3. Prior over opponent behaviour

Choose a finite catalogue of `L` card policies. A policy includes its identity
and any configuration that affects selection. For example, candidates could
include lowest card, highest card, closest gap, lowest fitting card, and hand
flexibility.

With no information favouring any candidate:

```text
P(z_j = k) = 1 / L
```

This is uniform over the catalogue, not over all imaginable behaviours. Avoid
duplicate or nearly identical entries: many variants of one strategy would
collectively give that behaviour more initial probability.

Treat `epsilon_j` as continuous, with a Beta prior independent of the initial
policy choice:

```text
epsilon_j ~ Beta(alpha_0, beta_0)
alpha_0 = beta_0 = 1
p(z_j = k, epsilon_j = e) = (1 / L) × BetaDensity(e; 1, 1)
```

`Beta(1,1)` is the uniform density on `[0, 1]`: equal-length intervals receive
equal probability. There is no epsilon grid. Prior draws lie strictly inside
the interval with probability one, giving every card in a hypothetical hand
positive selection probability.

A continuous prior gives zero mass to exactly zero and exactly one. It can
concentrate arbitrarily close to those endpoints but cannot conclude a positive
posterior probability of perfect adherence or pure random play as distinct
point hypotheses. Explicit endpoint masses are a possible later extension,
not part of the initial model.

At match start, the priors for different opponents are independent and
independent of the deal. Within a match, each opponent's policy and epsilon are
assumed fixed. Evidence updates our beliefs about them. This first version does
not model an opponent changing strategies over time.

### Opponent-model modes

A policy mixture is a component of this model, not a separate strategy. An
evaluator such as model-based bait or penalty minimisation can select one of
three modes:

| Mode | How policy uncertainty is handled |
| --- | --- |
| `single_policy` | Fix one configured policy for each opponent |
| `fixed_mixture` | Keep equal policy weights throughout the match |
| `learned_mixture` | Start with equal policy weights and learn from observed play |

The Bayesian updates in this document describe `learned_mixture`. To define a
precise comparison, let z denote the joint assignment of policies to opponents
and factor the learned posterior as:

```text
b_t(H, epsilon, z) = P(z | I_t) × b_t(H, epsilon | z)
```

The fixed-mixture model instead predicts using:

```text
b_fixed_t(H, epsilon, z) = W_0(z) × b_t(H, epsilon | z)
W_0(z) = product over opponents j of (1 / L)
```

It retains learning about hands and epsilon conditional on each policy
assignment, but does not let the assignments' marginal likelihoods change their
mixture weights. This is a deliberately constrained prediction model, not the
unrestricted Bayesian posterior. Merely skipping a policy update in joint
particle inference would not guarantee these fixed weights.

Single-policy mode uses the same construction with all weight on one configured
joint assignment. Epsilon remains continuous and learned even in this mode:
one assumed base policy need not imply deterministic choices.

For a fixed-versus-learned comparison, keep the catalogue, epsilon prior and
learning method, hand-inference method, work budgets, horizon, row-choice rules,
and penalty objective the same. Conditional inference remains the same target;
the final marginal beliefs about hands and epsilon may change when policy
weights change. Report approximation quality if finite sampling does not
adequately cover the assignments needed by the fixed mixture.

## 4. A policy with random-choice probability

Let `pi_k(c | v)` be policy k's probability of choosing card c from observation
v. It is zero for cards outside the player's hand. For a deterministic policy it
is one for the selected card and zero for other cards.

For a hand of size `h > 0`, define:

```text
q(c | v, k, epsilon) =
    (1 - epsilon) × pi_k(c | v) + epsilon / h,  if c is in the hand
    0,                                         otherwise
```

The random branch chooses uniformly from the entire hand, including the card
the policy would have selected. Thus, for a deterministic policy:

```text
P(policy's preferred card) = 1 - epsilon + epsilon / h
P(each other card)         = epsilon / h
```

For example, with four cards and `epsilon = 0.2`, the preferred card has
probability `0.85`, and each other card has probability `0.05`. On the final
card, the probability is one regardless of policy or epsilon, so that choice
cannot distinguish policies or random-choice probabilities.

We call epsilon a random-choice probability. A large value may indicate a
policy missing from the catalogue, not poor play. At epsilon one, every policy
produces the same uniform distribution. A uniformly random base policy also
makes epsilon unidentifiable: observations cannot update epsilon conditional
on that base policy. Such a policy can still represent exactly uniform play,
whereas a nonuniform policy with a continuous epsilon can approach it without
placing probability mass on epsilon exactly equal to one.

Policy adapters must provide selection probabilities or an explicitly defined
approximation to them. One seeded action sampled from a stochastic bot is not
the likelihood of the observed action.

### Continuous posterior for epsilon

For a fixed hypothetical hand and policy, abbreviate the base policy's
probability of the observed card as `p` and the uniform probability as `u = 1/h`.
One observation updates the density as follows:

```text
f_new(e) proportional to f_old(e) × [(1 - e) × p + e × u]
```

The result generally is not a single Beta density. We do not observe which
branch generated the card; even the policy's preferred card may have been
chosen by the random branch.

Starting from a Beta(A, B) density, this update can be written exactly as:

```text
r = A × u / (B × p + A × u)
f_new(e) = (1 - r) × BetaDensity(e; A, B + 1)
           + r × BetaDensity(e; A + 1, B)
```

Here r is the posterior probability that this observation used the random
branch, conditional on the hypothesised hand and policy. With a uniform prior
and a four-card hand, observing a deterministic policy's preferred card gives
posterior mean `0.4`; observing a different card gives `Beta(2,1)`, with mean
`2/3`. On the final card, `p = u = 1`, so the likelihood is constant and the
entire epsilon density is unchanged.

Repeated observations produce mixtures, also averaged over uncertain hands and
policies. Updating one Beta distribution with fractional branch counts is not
the exact posterior. The first implementation instead represents epsilon by
continuous values in joint particles and uses posterior-preserving
rejuvenation to explore values not present in the initial sample.

## 5. Learning from a simultaneous reveal

Immediately before a reveal, suppose a sampled world contains hidden hands
`H_{-i,t}` and behaviour parameters theta. Conditional on that world and the
pre-reveal information, opponents' random choices are modelled as independent:

```text
L_t(H, theta) = product over j != i of
    q(a_{j,t} | v_{j,t}(H), z_j, epsilon_j)
```

Each `v_{j,t}` contains that opponent's hypothetical hand and the information
available before selection. It does not contain our chosen candidate or other
players' current unrevealed cards. Never evaluate historical choices on the
board after that turn's placements.

Bayes' rule gives the posterior before removing the revealed cards:

```text
b_t_updated(H, theta) = b_t(H, theta) × L_t(H, theta) / Z_t
Z_t = sum over H and policy assignments z of
      integral over epsilon in (0, 1)^(n-1) of
      b_t(H, z, epsilon) × L_t(H, z, epsilon) d epsilon
```

Then remove each revealed card from its owner's hypothetical hand. The engine
applies the observed placements and captures to obtain the next public board.
Our own actions are decisions we made, not additional likelihood evidence about
the opponents.

Policy evidence must be averaged over possible hands. For example, a play of 40
supports lowest-card behaviour in a hand `{40, 70, 90}`, but highest-card
behaviour in a hand `{10, 25, 40}`. Neither interpretation follows from the
revealed number alone.

Even though opponent priors are independent, the posterior need not factorise:
exclusive card ownership links possible hands, and those hands link the policy
inferences. Preserve these relationships in the joint sampled worlds. Individual
policy summaries are marginals of that joint belief:

```text
P(z_j = k | I_t) = integral/sum of posterior mass where z_j = k
E[epsilon_j | I_t] = integral/sum of epsilon_j × posterior mass
```

The integrals are over continuous epsilon values and the sums are over discrete
policy assignments and hidden allocations. Particle estimates replace these
operations with weighted sums.

## 6. Row choices and deterministic transitions

Given a joint selection, resolve cards from lowest to highest using the engine.
Placement, automatic sixth-card captures, score changes, and hand termination
are deterministic. When a low card requires a row choice, the first predictive
model takes the cheapest row on the board at that moment, breaking ties by row
index. Our evaluator uses the row rule of its configured continuation policy
for its own simulated and actual choices, including choices in the current turn.
Cheapest row is a simple choice for this policy but is not the only permitted one.

During learning, use the **observed** row choices to reconstruct subsequent
boards, even if a player chose a more expensive row. We are modelling card-choice
likelihood conditional on observed public transitions; row choices do not yet
contribute a likelihood term. An unexpected row choice therefore does not erase
the card-policy posterior. Cheapest-row behaviour is a prediction assumption
that can be wrong, and should be evaluated separately.

This distinction avoids claiming a fully learned generative model of every
action. A future row-policy model can add its own parameters and likelihoods.

## 7. New deals and persistent learning

At the hand boundary, discard old hidden-card allocations. Retain the joint
posterior over theta and sample the next deal uniformly, conditional on our new
hand and the public row starts:

```text
P(new hidden deal, theta | information at the new deal)
    = P_uniform(new hidden deal | our cards, row starts)
      × P(theta | previous hands)
```

Card knowledge is hand-specific. Behavioural evidence persists across hands in
the same match. Start a new prior for a new match unless an experiment explicitly
defines persistent player identities and cross-match learning.

## 8. Finite approximation and recovery

The exact posterior is too large to enumerate. Approximate it with a fixed
budget of weighted particles:

```text
particle m = (hidden allocation m, theta m, weight w_m)
sum_m w_m = 1
```

Update likelihoods in log space. Monitor effective sample size:

```text
ESS = 1 / sum_m(w_m^2)
```

Resampling allocates effort to plausible worlds, but copying particles alone
does not restore lost diversity. Use observation-consistent proposals or
posterior-preserving rejuvenation to introduce alternative allocations and
behaviour parameters.

Each particle carries a continuous epsilon value per opponent. Resampling
alone can only copy those values. For epsilon rejuvenation, one option is a
Metropolis-Hastings random walk in logit coordinates:

```text
x = log(e / (1 - e))
x_proposed = x + Normal(0, proposal_scale^2)
e_proposed = logistic(x_proposed)
g(e) = BetaDensity(e; 1, 1) × behavioural likelihood of all observations
acceptance = min(1,
    g(e_proposed) × e_proposed × (1 - e_proposed)
    / [g(e) × e × (1 - e)])
```

This move conditions on the particle's policy and hidden allocations. The
`e × (1 - e)` factors are the change-of-variable Jacobian; omitting them would
target the wrong posterior. Evaluate the ratio in log space with numerically
stable logit/logistic operations. Configure a positive proposal scale and a
fixed number of moves. A finite number of moves does not guarantee well-mixed
or independent posterior samples; monitor diversity and sensitivity to budget.

Use the full accumulated likelihood when rejuvenating parameters retained
across hands. A finite hand-start particle population has an atomic empirical
distribution; arbitrary jitter cannot preserve it while also introducing new
epsilon values. Retain enough information to evaluate the underlying continuous
posterior, rather than treating those atoms as the mathematical prior.

A useful first implementation can rebuild a fixed batch from the current
hand's history at each decision:

1. Save the posterior over theta at the start of the hand.
2. Record our original hand, the original four row starts, and every observed
   opponent card with its owner and play number.
3. Draw theta from that saved hand-start distribution. Allocate the remaining
   unknown cards uniformly to opponents' remaining slots and undealt slots.
   Add each opponent's already-played cards to reconstruct their original hand.
4. Replay the recorded public sequence, evaluating all card-choice likelihoods
   on the reconstructed pre-selection observations.
5. Weight each sample by the product of this hand's likelihoods and normalise.

This proposal already conditions on revealed ownership, which avoids rejecting
almost every sample merely because several opponents revealed cards at once.
Under the uniform deal prior, conditioning on those ownership constraints has
the same normalising constant for every theta. The remaining importance weight
is therefore the behavioural likelihood product. If a different proposal is
used, include the appropriate target-to-proposal probability ratio.

Use the saved hand-start distribution in this rebuild, not the current posterior
followed by the full history again: that would count the same evidence twice.
The finite representation of the hand-start posterior still limits which
behavioural hypotheses a rebuild can recover; preserve parameter diversity too.

For completed hands, all ten cards played by every opponent have become public.
For the initial board-and-hand policy catalogue, we can therefore reconstruct
their historical hands and retain the per-policy likelihood factors from those
hands. Combining these factors with current-hand hypotheses lets rejuvenation
evaluate new epsilon values against all observed play. We must not restart from
`Beta(1,1)` and only the current hand's likelihood at each new deal.

Epsilon above zero helps with unexpected choices, but cannot make an impossible
card allocation possible. Recovery must reconstruct allocations consistent with
the observations. If numerical or sample-support problems persist, report the
condition and use an explicitly configured fallback action policy. Do not
silently discard the history or report a confident posterior after a reset.

Every rebuild, resampling, and recovery operation must have a fixed work budget
and use private seeded randomness. Wall-clock cutoffs are not a reproducible
sampling budget.

## 9. Predicting and comparing our candidate cards

For each decision, draw weighted worlds from the posterior. In each world,
sample opponents' cards from their noisy policies before varying our candidate.
For each card c in our hand, resolve that same opponent selection with c using
the engine and the specified row-choice rules.

Let `Y(c)` be the number of bull heads we take during this turn. The model
estimates its distribution, conditional on choosing c:

```text
P(Y(c) = y | I_t)
    = sum/integral over hidden worlds of b_t(world)
      × P(engine outcome costs y | world, our choice c)
```

Use the same sampled worlds and opponent selections for every candidate.
Opponents cannot adapt their simultaneous choice to our candidate. If future
row-choice policies become stochastic, share their random streams while still
evaluating each choice on the candidate-specific board.

For empirical outcomes `y_m(c)` with normalised weights `w_m`, supported
objectives are:

| Objective | Quantity to minimise |
| --- | --- |
| Mean penalty | `sum_m w_m × y_m(c)` |
| Pickup probability | `sum_m w_m × 1[y_m(c) > 0]` |
| Threshold exceedance | `sum_m w_m × 1[y_m(c) > N]`, for configured non-negative N |
| Upper-tail penalty | Weighted mean of the worst alpha probability mass, for `0 < alpha <= 1` |

For the upper-tail objective, sort outcomes by descending penalty and take
exactly alpha total mass, including a fractional boundary weight if needed;
divide the resulting weighted penalty by alpha. This avoids incorrectly treating
the entire boundary outcome as part of the requested tail.

Choose the candidate with the lowest objective, breaking exact ties by lowest
card value. The one-turn case above is `horizon: 1`, not a separate strategy.
It does not measure full-hand or match strength.

### Multiple-turn simulation

Define the horizon as the total number of simultaneous turns to resolve,
including the current turn:

| Setting | Meaning |
| --- | --- |
| `horizon: 1` | Current turn only |
| `horizon: 3` | Current turn and two subsequent turns |
| `horizon: remaining_hand` | Continue until this hand ends |

For a positive integer horizon H and r turns remaining, the effective horizon
is `h = min(H, r)`. For `remaining_hand`, it is `h = r`. Never deal a new hand
inside this evaluator. Longer horizons evaluate the configured continuation
policy, not an optimally chosen sequence of future actions.

Each rollout samples hidden hands and opponent parameters once from the belief
at the actual decision. Keep those hands, policy identities, and epsilon values
throughout that rollout. Remove cards as they are played; leave undealt cards
undealt. Draw fresh random-choice branches and policy action randomness on each
turn. Do not resample the true simulated deal or opponent type between turns.

After our candidate's first turn, our continuation policy chooses subsequent
cards. Every simulated player sees only its own hand and its available public
history; all choices within a turn are made before that turn's reveal. The
candidate changes later boards and observations, so later actions can differ
between candidate branches. Share random streams across branches where useful,
but do not force identical later actions on different boards.

The initial continuation policies can be existing board-and-hand bots. A learning
continuation would need its own belief updates from simulated observations;
those updates change its beliefs, not the underlying sampled world. It must not
read the simulator's privileged hands or opponent parameters. Simulated evidence
must never be written into the real bot's learned belief.

For rollout m, let `Y_{m,s}(c)` be our penalty at simulated turn s. Evaluate:

```text
G_m(c) = sum for s = 0, ..., h-1 of Y_{m,s}(c)
         + V_cutoff(observation after h turns)
```

Choose explicitly between `V_cutoff = 0` and a configured remaining-cost
estimator. Set it to zero at the hand boundary. Apply the selected penalty
objective to the distribution of `G_m(c)`, replacing the one-turn outcomes in
the formulas above. There is no discounting within the simulated hand.

A short horizon with zero cutoff value can reward postponing a penalty until
just after the cutoff. Remaining-hand simulation avoids that particular bias,
but still depends on sampled hands, opponent assumptions, and continuation
quality. A point estimate of expected remaining cost can help a mean-penalty
objective; it does not restore the distribution of omitted penalties. Tail-risk
or exceedance objectives need a predictive distribution of remaining cost, or
must be labelled as objectives on an approximate surrogate score rather than
calibrated full-hand penalty probabilities.

Model-based bait can reuse this evaluator while restricting candidates to
full-row bait cards and a configured fallback play. It keeps the fallback on
equal estimated mean penalty, as described in the existing strategy catalogue.

## 10. Configuration and observable diagnostics

Before implementation, expose these experiment settings explicitly:

| Setting | Contract |
| --- | --- |
| Opponent-model mode | `single_policy`, `fixed_mixture`, or `learned_mixture`; mixture modes start equally weighted |
| Candidate policies and their options | Nonempty, nonredundant catalogue; equal initial policy weights |
| Epsilon prior | Continuous `Beta(1,1)` per opponent, independent of initial policy; no grid or endpoint masses |
| Particle and predictive sample budgets | Positive fixed counts |
| Horizon | Positive total turn count including this turn, or `remaining_hand`; capped at the hand boundary |
| Continuation policy and options | Our later card choices and our actual/simulated row-choice rule; later card choices are unused at horizon 1 |
| Cutoff evaluation | Explicit zero estimate or a named remaining-cost estimator with options; zero at hand end |
| Resampling/rejuvenation settings | Explicit resampling trigger, positive logit proposal scale, and fixed rejuvenation work budget |
| Recovery fallback | Explicit policy and options |
| Penalty objective | Explicit identity and required parameters |
| Sampling seed | Private deterministic stream independent of the hidden game seed |

The initial opponent row policy is cheapest row; it is a versioned prediction
assumption, not a parameter learned from card play. One-turn and longer-horizon
configurations share the same simulator, opponent-model modes, and objectives.
Record all resolved settings and model/policy versions with each experiment.

Diagnostics should include per-opponent policy marginals, the distribution over
epsilon, ESS, recovery counts, sample counts, and candidate penalty estimates.
Label these as beliefs conditional on the model, not discovered ground truth.
For epsilon, report credible intervals as well as means; a mean alone can hide
uncertainty or multiple explanations. No diagnostic should claim positive
probability at exact endpoints under this continuous prior.

Evaluate learning with predictions recorded **before** the next reveal. Useful
checks include predictive log loss, probability calibration, and comparison
against uniform-random predictions and a fixed equal-weight policy model.
Controlled opponents with known policies and epsilon values can test recovery
when parameters are identifiable. Compare arena scores and unfinished outcomes
too; improved card prediction need not improve every penalty objective.

## 11. Integration with this project

The engine remains the source of truth for resolution. The probabilistic layer
owns hidden-world sampling, behavioural likelihoods, posterior updates, and
candidate evaluation. The arena continues to own execution and operational
limits. No probabilistic component should inspect privileged arena state.

The current `MatchView` retains 20 revealed plays in `play_history`, plus
`revealed_this_hand`. A learning bot must accumulate the public observations it
receives, deduplicate them by hand/play and event content, and preserve enough
pre-selection context to replay the current hand. It must not treat overlapping
history windows or repeated calls as new evidence. An incomplete history must
be detected rather than silently described as the whole match.

For this first catalogue of board-and-hand policies, current-hand replay plus
the hand-start parameter posterior is sufficient. Policies with additional
memory would require reproducing that memory from information they could
actually observe. Public records cannot reconstruct opponents' private message
histories.

Related guides: [Game rules and information](game-rules.md),
[Writing bots](bots.md), [Strategy catalogue](strategy-families.md), and
[Traces and replay](traces.md).
