# Strategy catalogue for 6 nimmt! experiments

## Purpose

Compare reproducible strategies by the ideas they test, rather than arranging them
in tiers of expected strength. More elaborate reasoning does not necessarily
produce a stronger player. The board-independent baselines and board-and-hand heuristics are implemented;
see [Writing bots](bots.md) for their registry names. The remaining families
describe candidates and experimental variants.

## Gameplay and common conventions

A complete strategy owns two connected decisions: which card to play and which
row to take when its card is below every current row end. Players select cards
simultaneously, then revealed cards resolve from lowest to highest. Becoming the
sixth card in a row causes an automatic pickup with no row choice.

Card selection can deliberately invite a pickup. Row selection affects remaining
placements this turn and future plays. The two decisions can share reasoning and
plans; reconsider any planned row choice using the actual board when it arrives.

In this document:

- **Applicable row** means the row with the greatest end card below the candidate.
  Players cannot freely place a card onto another row.
- **Currently fitting** means the card is above at least one row end and its
  applicable row contains fewer than five cards. It would avoid a pickup if placed
  immediately onto that board. This is what the earlier term “safe card” meant;
  it does not guarantee avoiding a pickup after opponents’ cards resolve.
- **Immediate cost** means bull heads taken if the candidate resolves against the
  current board. Below all row ends, use the row the strategy would choose.
- **Cheapest-row rule** means taking the fewest bull heads, breaking ties by row
  index. Every baseline always uses this rule when a row choice is required,
  including random-card and board-and-hand baselines. Only strategies explicitly
  investigating alternative row selection depart from it.

Every implementation must specify fallbacks, tie-breaking, row selection, and
configuration. Random and simulation strategies use private seeded randomness
and fixed computational budgets for reproducibility.

For example, a row `[10, 20, 30, 40]` can currently accept `43`. If an
opponent’s `42` lands there first, it fills the row and `43` takes it. Conversely,
an earlier pickup can make a card fit that would have taken a row on the original
board. Current fit and predicted pickup risk are different assessments. In the
timing strategies, “safer” means lower estimated pickup risk or cost, not certainty.

## Families

### Board-independent baselines

Choose cards without evaluating the board. These establish the value of disposal
order and provide simple reference opponents. Their row decisions may still use
the board.

### Board-and-hand heuristics

Use immediate board features or the composition of the remaining hand. These
include retaining distributed responses to unknown future boards without explicitly
simulating those boards.

### Pickup and row-shaping tactics

Reason about accepting penalties and replacing rows. These include deliberate
pickups, anticipating someone else's pickup, and choosing a replacement that helps
the remaining hand.

### Uncertainty-aware evaluation

Evaluate possible opponent plays or future situations under an explicit model.
Vary information, opponent assumptions, and risk objectives independently where
possible.

### Timing and planning

Consider when cards should be used and the consequences of holding them. This
ranges from simple urgency heuristics to multi-turn simulation.

Families overlap. Each strategy below has a primary family for navigation; this
is neither a strength ranking nor an inheritance hierarchy.

## Strategy comparison table

| Strategy or experimental variant | Primary family | What it covers or tests |
| --- | --- | --- |
| Random card | Board-independent baselines | Reference performance with uniform random card selection and cheapest-row pickup |
| Lowest card | Board-independent baselines | Ascending disposal order |
| Highest card | Board-independent baselines | Descending disposal order |
| Closest gap | Board-and-hand heuristics | Value of proximity to an applicable non-full row end |
| Coldest row | Board-and-hand heuristics | Value of row capacity without prioritising distance |
| Lowest fitting card | Board-and-hand heuristics | Low-card disposal among cards that fit the current board |
| Highest fitting card | Board-and-hand heuristics | Effect of reversing currently fitting-card disposal order alone |
| Hand flexibility | Board-and-hand heuristics | Value of distributed options rather than clusters or extremes alone |
| Controlled burn | Pickup and row-shaping tactics | Whether a cheap deliberate pickup now helps avoid larger costs later |
| Bait: count threshold | Pickup and row-shaping tactics | Whether the number of possible intervening opponent cards is a useful signal for attempting bait |
| Bait: model-based | Uncertainty-aware evaluation | Whether predicted pickup cost identifies bait plays better than a count threshold |
| Hand-aware row choice | Pickup and row-shaping tactics | Immediate row cost versus usefulness of the replacement for the remaining hand |
| Penalty-distribution evaluator | Uncertainty-aware evaluation | Effect of configurable penalty objectives and prediction models using full public play history |
| Timing-aware selection | Timing and planning | Value of urgency estimates, comparing currently fitting candidates with all-card evaluation |
| Simulation evaluator | Timing and planning | One-turn Monte Carlo through remaining-hand rollouts under a configurable horizon and continuation policy |

Single-policy, fixed-mixture, and learned-mixture assumptions are opponent-model
modes available to the evaluators, not separate player strategies to implement.
Penalty objectives are configurations of the penalty-distribution evaluator.
Neither requires a separate architectural family.
One-turn Monte Carlo and longer rollouts are configurations of the same
simulation evaluator, not separate strategies. Its horizon counts total turns
including the current turn.

## Strategy definitions

### Board-independent baselines

| Strategy | Decision rule | Details and caveats |
| --- | --- | --- |
| Random card | Choose uniformly at random from the hand. | — |
| Lowest card | Play the lowest card in hand. | — |
| Highest card | Play the highest card in hand. | — |

The original five card-selection proposals were random card, lowest card, highest
card, closest gap, and lowest fitting card. All five always choose the cheapest row
when required; their differences concern card selection only.

### Board-and-hand heuristics

| Strategy | Decision rule | Details and caveats |
| --- | --- | --- |
| Closest gap | Choose the smallest positive gap to the applicable row end, considering only rows with fewer than five cards. | Break ties by lowest card. If none fits, minimise immediate pickup cost, then choose the lowest card. |
| Coldest row | Consider only cards that currently fit. Prefer the card whose applicable row contains the fewest cards, breaking ties by lowest card value. | If no card currently fits, minimise immediate pickup cost, then choose the lowest card. When a row choice is required, take the cheapest row, breaking ties by row index. |
| Lowest fitting card | Play the lowest card that currently fits. | If none fits, minimise immediate cost, breaking ties by lowest card. |
| Highest fitting card | Use the same current-board eligibility and fallback as lowest fitting card, but choose the highest eligible card. | — |
| Hand flexibility | Among equally cheap immediate plays, prefer retaining cards distributed through the number range rather than clustered together. | Keeping only the extremes does not guarantee middle options. |

Coldest row shares the fallback and low-card disposal preference of lowest
fitting card. Comparing them isolates the effect of preferring a less occupied
row; coldest row does not use gap or row penalties to break occupancy ties.

#### Hand flexibility details

| Setting | Meaning | Constraints |
| --- | --- | --- |
| Coverage measure `D(H)` | Average distance from each value in `1..104` to its nearest card in remaining hand H | Lower is better; include all values, even seen cards |
| Selection order | Minimise immediate pickup cost, then D after removing the candidate, then card value | Coverage never justifies extra immediate penalties |
| Empty remaining hand | Skip D on the final card | The measure requires a nonempty hand |
| Row choice | Take the cheapest row | Break ties by row index |

```text
D(H) = sum(min(abs(x - h) for h in H) for x in 1..104) / 104
```

These are fixed rules, not configurable parameters. The measure rewards breadth
and distribution, but treats values equally and distance symmetrically. It ignores
row occupancy and opponent plays: **lower D does not imply lower pickup risk**.
Retaining coverage can also waste a cheap disposal opportunity; timing strategies
explore that trade-off. Evaluation against sampled future boards is a separate
possible extension.

### Pickup and row-shaping tactics

| Strategy | Decision rule | Details and caveats |
| --- | --- | --- |
| Controlled burn | When the cheapest row costs at most K and a card is below every current row end, play a low card to invite that cheap pickup. | Otherwise delegate card selection to the configured fallback strategy. K and the fallback strategy are configurable; proposed K values to test are 3, 5, and 8. Evaluate the actual row costs again when the row choice arrives; intervening placements can change the opportunity. Future savings are a hypothesis, not a guaranteed benefit. |
| Bait: count threshold | Consider cards whose applicable row currently has five cards. Attempt bait only if the count of possible opponent cards strictly between that row end and the candidate reaches a configurable threshold. | Exclude cards known to be unavailable to opponents. If no candidate qualifies, delegate card selection to a configurable fallback strategy. Candidate ranking and the threshold default remain to be specified. |
| Hand-aware row choice | Weigh immediate penalties against how replacing each row affects the remaining hand. | A slightly more expensive row might create useful placements, but opponents can also use or change it. First compare against the cheapest-row rule with card selection held fixed where practical; a coherent combined player can later anticipate this row policy during card evaluation. |

#### Count-threshold bait configuration

| Parameter | Meaning | Constraints |
| --- | --- | --- |
| `intervening_card_threshold` | Minimum possible intervening-card count that permits bait | Positive integer; inclusive threshold |
| `fallback_strategy` | Card-selection strategy when no bait candidate qualifies | Configurable strategy with its own options; receives the same player observation |

Count values strictly between the full row's end and the candidate, excluding the
player's hand, current board, and this hand's revealed cards. The count ignores
opponent numbers and preferences; possible cards may be undealt or unplayed.
An earlier pickup does not guarantee safety. Always choose the cheapest row when
required. Defaults remain to be selected.

#### Controlled-burn configuration

| Parameter | Meaning | Constraints |
| --- | --- | --- |
| `K` | Maximum current cheapest-row cost that permits a deliberate pickup | Non-negative integer in bull heads; inclusive threshold |
| `fallback_strategy` | Card-selection strategy when the burn condition does not hold | Configurable strategy with its own options; receives the same player observation |

The fallback is configurable, not fixed to lowest fitting card. Always choose the
cheapest row when required, regardless of the fallback. Record K and the fallback's
identity and options with each experiment. Defaults remain to be selected.

### Uncertainty-aware evaluation

| Strategy | Decision rule | Details and caveats |
| --- | --- | --- |
| Bait: model-based | Under an explicit opponent model, estimate this turn's pickup cost for candidates targeting full rows and for the configured fallback play. Attempt bait only if the best bait candidate has strictly lower expected cost than the fallback play. | Model joint placements, row choices, and possible repeated pickups. Equal estimated cost keeps the fallback play. Opponent model, evaluation budget, candidate tie-breaking, and defaults remain to be specified. |
| Penalty-distribution evaluator | Predict each candidate's distribution of bull heads taken this turn using the configured model, then minimise the configured objective. | Model and objective are independently configurable. Expected-bull minimisation is the mean-penalty objective, not a separate strategy. Use full public play history; objective ties and defaults remain to be specified. |

#### Penalty-distribution evaluator configuration

| Parameter | Meaning | Constraints |
| --- | --- | --- |
| `model` | Underlying model of opponent hands and plays used to evaluate candidate outcomes | Configurable; no particular hand-belief model or calculation method is prescribed |
| `model_options` | Settings for the selected model | Validated by that model; record alongside its identity for reproducibility |
| `objective` | Function mapping each predicted penalty distribution to a value to minimise | Configurable independently of the model; choices are defined below |
| `objective_options` | Parameters required by the selected objective | Validated by that objective; record alongside its identity |

The model receives the current board, own hand, and complete public play history,
including previous reveals and pickups. It must not receive opponents' hidden
hands or unrevealed selections. Track known cards within the current hand and reset
that card knowledge at each deal; earlier hands may still inform opponent behaviour.
Models may use exact probabilities or seeded sampling. Their assumptions about
hands and selection policies can vary without changing the selected objective.
Use the same configured model when comparing risk objectives or bait decisions.

#### Penalty objectives

| Objective | Value to minimise | Parameters and constraints |
| --- | --- | --- |
| Expected penalty | Mean bull heads taken | No parameters; risk-neutral expected-bull minimisation |
| Pickup probability | Probability of taking more than zero bull heads | No parameters |
| Threshold exceedance | Probability of taking more than `N` bull heads | `N` is a non-negative integer; minimise the probability rather than filtering candidates |
| Upper-tail penalty | Mean penalty in the worst `tail_fraction` of predicted outcomes | `0 < tail_fraction <= 1`; include only the required probability mass at the boundary |

Threshold exceedance still ranks candidates when every card can exceed N; it does
not require a separate threshold-failure fallback. A finite sample's maximum is
not a true worst-case bound, so worst-case search is not included among these
objectives. Defaults remain to be selected.

#### Model-based bait

Use a configurable fallback strategy with its own options. Evaluate its proposed
card and bait candidates using the same information, opponent model, horizon, and
cost objective. Unlike the count-threshold variant, this trigger accounts for
opponent count and selection behaviour through the model rather than requiring a
minimum interval count. Use the cheapest-row rule for actual row choices and for
the player's own row choices during evaluation.

This is a tactical restriction of expected-cost evaluation: compare full-row bait
candidates with the fallback play, rather than searching all cards for the best
expected cost. Predicted improvement is not a guarantee of avoiding a pickup.
Compare the two bait variants with the same fallback and card knowledge to assess
the value of the additional modelling.

#### Opponent-model modes

The [probabilistic gameplay model](uncertainty-model.md) separates beliefs about
hidden hands, card-selection policies, and each opponent's continuous
random-choice probability epsilon. The same prediction interface supports:

| Mode | Policy weights | Purpose |
| --- | --- | --- |
| Single assumed policy | All weight on one configured policy per opponent | Test a specific behavioural assumption |
| Fixed equal-weight mixture | Equal weights over the selected policies, held fixed | Represent policy uncertainty without learning its weights |
| Learned mixture | Start equally weighted, then update from observed choices | Adapt predictions to each opponent |

In all three modes, hidden-hand beliefs and epsilon can still learn from play.
The fixed mixture retains inference conditional on each policy assignment but
keeps the assignment weights fixed. It is an explicit experimental comparison,
not the fully Bayesian posterior with one update accidentally omitted. The
model document defines this distinction mathematically.

Fixed-mixture mode remains useful as a baseline: it tests whether learning policy
weights improves predictions and arena outcomes, particularly with limited
evidence or opponents whose behaviour changes. It does not require a separate
bot. Model-based bait, penalty-distribution evaluation, timing-aware selection,
and rollouts can all consume these model modes.

#### Shared modelling considerations

Counting possible intervening cards can inform predictions, but an intervening
opponent card does not automatically imply a pickup. Row capacity, placement order,
resets, and opponent preferences matter. Account for unseen cards that are undealt;
do not assign all unseen cards to opponents. Reset card knowledge with each deal.

The proposed learned model updates policy weights and continuous epsilon
jointly with possible hidden hands. Publicly played cards do not reveal what
alternatives opponents held, so adaptation must account for that uncertainty.
Uniform initial deals do not imply uniform remaining hands after behavioural
evidence. See the model document for priors, likelihoods, inference, and recovery.

### Timing and planning

| Strategy | Decision rule | Details and caveats |
| --- | --- | --- |
| Timing-aware selection | Estimate each card's play-now and delayed costs, then select using the configured urgency rule below. | Combines closing-opportunity and improving-opportunity reasoning. The model, delay horizon, candidate restriction, and urgency weight are configurable. |
| Simulation evaluator | For each candidate, sample possible opponent hands and simulate play under specified continuation policies to the configured horizon. Choose the candidate with the lowest configured penalty objective. | `horizon: 1` is one-turn Monte Carlo; larger horizons are rollouts. Specify card and row decisions and any cutoff evaluation. |

#### Timing-aware selection configuration

| Parameter | Meaning | Constraints |
| --- | --- | --- |
| `model` and `model_options` | Estimate expected bull heads taken by a card now and if played after a delay | Use the same cost units and full public play history; specify assumptions about intervening plays and what happens if the hand ends before the horizon |
| `delay_horizon` | Number of subsequent turns before the held card is evaluated | Positive integer |
| `candidate_mode` | Restrict evaluation to currently fitting cards or evaluate all cards | `fitting_only` or `all_cards`; selection rules below |
| `urgency_weight` | Strength of urgency relative to immediate cost in `all_cards` mode | Non-negative number; unused in `fitting_only` mode |

```text
urgency(card) = delay_cost(card) - play_now_cost(card)
```

Positive urgency means holding the card is predicted to make it more costly;
negative urgency means waiting may improve it.

| Mode | Selection rule |
| --- | --- |
| `fitting_only` | Among cards that currently fit, maximise urgency. This captures the former expiring-safety heuristic. |
| `all_cards` | Minimise `play_now_cost(card) - urgency_weight * urgency(card)`. Sufficient urgency can justify taking penalties now. |

This is a timing heuristic, not a full comparison of alternative play sequences.
The two card-cost estimates do not by themselves account for the cost of other
cards played while waiting; explicit sequence evaluation belongs to lookahead.
Defaults, tie-breaking, and the fallback when no card fits in `fitting_only` mode
remain to be specified. Actual row choices use the cheapest-row rule.

#### Simulation evaluator configuration

| Parameter | Meaning | Constraints |
| --- | --- | --- |
| `horizon` | Total turns to simulate, including the current turn | Positive integer or `remaining_hand`; 1 means this turn, 3 means this turn plus two more; cap at the hand boundary |
| `model` and `model_options` | Beliefs about opponent hands and behaviour | Use full public history; respect hand sizes, card uniqueness, and the undealt remainder |
| `continuation_policy` | Our decisions after the candidate play | Specify card and row selection; following a policy does not optimise all possible future sequences |
| `opponent_policies` | Simulated opponents' subsequent decisions | Use policies and epsilon drawn from the opponent model; specify row-choice behaviour |
| `objective` and `objective_options` | Score each candidate's distribution of accumulated penalties | Mean, pickup probability, threshold exceedance, or upper-tail penalty |
| `cutoff_evaluation` | Estimate cost after a fixed cutoff | Explicitly choose no estimate or a named estimate with options; zero at the hand boundary |
| `sample_count` | Rollout samples per candidate | Positive integer; fixed budget with seeded randomness |

Use the same sampled worlds when comparing candidates. Simulated players,
including our continuation policy, must act only on their own observations, not
hidden sampled hands. Otherwise rollouts credit them with information unavailable
in actual play.

Within a continuation, hands persist and shrink; do not redraw them at each
turn. Sample each opponent's policy and epsilon once per world and keep them
fixed, drawing fresh action randomness at each turn. Later decisions use the
candidate-specific board and simulated public history. Our continuation policy
chooses later cards; following it does not optimise all possible future sequences.
Use its row-choice rule consistently for actual and simulated row choices,
including during the current turn. A one-turn configuration needs no later
card decisions but still needs a row-choice rule.

A fixed cutoff can reward postponing penalties beyond the horizon; an estimate of
remaining cost can address this. Remaining-hand rollouts reach the hand boundary
and need no estimate of unplayed cards. Break equal objective values by lowest
card value. Horizons, continuation policies, objectives, cutoff treatment, and
budgets are explicit experiment settings. Timing-aware selection stays separate:
it scores urgency rather than accumulated consequences of continuations.

#### Shared timing and planning considerations

Play-window awareness distinguishes current risk from expected change in risk:

| Current assessment | Likely safer if held | Likely riskier if held |
| --- | --- | --- |
| Fits the current board | Potentially save it | Consider using the opportunity now |
| Would take a row on the current board | Potentially wait | Consider disposal before it gets worse |

These are tendencies to investigate, not fixed rules. Pickups reset rows and can
abruptly change which cards are useful. Flexibility asks which options remain;
timing asks which options lose or gain value if the player waits.

Monte Carlo is a method, not a strength ceiling. Matching one planner does not
establish that the game is shallow: sample budget, model errors, rollout policy,
and horizon all affect performance.

## Attributes shared across families

Record these separately for each complete player:

| Attribute | Example choices |
| --- | --- |
| Information | Current board, own hand, and full public play history available; simple heuristics may use only the inputs their rules require |
| Opponent model | Single assumed policy; fixed equal-weight mixture; learned mixture |
| Horizon | Current turn; several turns; remaining hand |
| Objective | Expected penalties; pickup probability; threshold exceedance; upper-tail penalty |
| Computational budget | Fixed sample count or search expansion count |

Full public play history is available to evaluation models by default. Card
knowledge limits possible holdings but does not determine which cards opponents
prefer to play; that inference belongs to the configurable model.

## Suggested comparisons

1. Establish the original five card-selection baselines, all using the same
   cheapest-row rule. Randomness in the random baseline applies only to cards.
2. Compare lowest versus highest fitting card, keeping all other rules fixed.
3. Prioritise hand flexibility, timing-aware selection in `fitting_only` mode,
   and hand-aware row choice. These isolate retaining options, urgency, and shaping the board.
4. Compare timing-aware selection modes and urgency weights with the model and
   horizon fixed, testing the effect of admitting cards that do not currently fit.
5. Hold an evaluator fixed while varying the underlying model, its options, or the risk objective.
   Compare fixed equal-weight and learned mixtures with the same policy catalogue,
   epsilon prior and learning method, hidden-hand inference method, sampling
   budget, horizon, row-choice assumptions, and penalty objective. The resulting
   hand beliefs may differ because policy and hand inference interact. Measure
   predictions made before reveals as well as arena outcomes.
6. Hold the model and evaluation approach fixed while varying planning horizon;
   report computational budgets alongside performance.
7. Test combinations after individual effects are understood. Flexibility, urgency,
   and disposal can disagree about which card to retain.

Use explicit seeds, consistent match settings, multiple deals, and balanced seat
assignments. Record configurations and budgets. Report unfinished or failed matches
alongside performance, and use several opponent lineups: strength can depend on
who a player faces.
