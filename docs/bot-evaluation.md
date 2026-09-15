# Experiments for evaluating 6 nimmt! bots

This report defines experiments to compare bots and strategies, learn when to
use or counter them, and produce practical advice for human play. The agreed
setting is **four or five players, playing to 66**, usually within a familiar
group or a subset of it.

There are two separate objectives: **winning a normal game**, and **finishing
high enough for a good picking position**. For picking orders, the agreed target
is the top two with four players and the top three with five. Score ties are
broken randomly; evaluation uses each tied player's exact qualifying chance.

The questions and these objectives are agreed. The experiments below are a
proposed programme, not experiments already run. Concrete starting settings
are specified so the programme can be reviewed and implemented. The arena audit
refers to repository commit `2d7a611813321ca52f75758d54436c251d341930`.

The implemented arena supports two to ten players. The four- and five-player
focus here describes this study, not a runtime restriction. Arena budgets count
actual games; seat balancing runs across fresh deals within that budget.

## 1. Evaluation questions

1. **Which bots and strategies perform best generally?** Across a varied set of
   plausible opponent combinations, which most often win, and which most often
   finish in the top half? Are there clear leaders or meaningful trade-offs?
2. **How does the opponent mix change what works?** Which strategies benefit or
   suffer against particular behaviours? What changes when several opponents use
   similar tactics, or when the table contains different combinations?
3. **When does each strategic idea help—and when does it hurt?** For the ideas
   represented by the heuristic and composed strategies, identify relevant
   conditions: the hand, board, remaining cards, current scores, and stage of
   the match.
4. **How should you combine tactics and change approach during a game?** Can
   switching tactics according to the situation outperform consistently
   following one strategy? How should those choices differ between aiming to
   win and aiming for the top half?
5. **How can you recognise and counter an opponent's behaviour?** What observable
   patterns justify changing your play, what responses work, and how much
   evidence do you need? Include opponents who mix tactics, rather than
   requiring identification of one exact bot policy.
6. **How vulnerable are those strategies and counters themselves?** What happens
   when opponents adapt, when their behaviour is misinterpreted, or when a
   different subset of the usual group plays? Which approaches remain dependable?
7. **What practical advice can we take to a human game?** Which findings can
   become understandable rules of thumb, with recognisable conditions and
   exceptions? How much benefit survives when the advice is simplified enough
   to use without computation?

## 2. Experiments

| Experiment | Questions answered | Main comparison |
| --- | --- | --- |
| [1. Strategy strength across opponent compositions](#experiment-1-strategy-strength-across-opponent-compositions) | 1, 2, 6 | Random and controlled lineups produce both matchup estimates and population summaries |
| [2. When a tactic helps](#experiment-2-when-a-tactic-helps) | 3 | A tactic enabled versus disabled at the same decision |
| [3. Combining and switching tactics](#experiment-3-combining-and-switching-tactics) | 1, 4 | Fixed, composed, and situation-dependent policies under both objectives |
| [4. Recognition and counterplay](#experiment-4-recognition-and-counterplay) | 5, 6 | The value of opponent inference, including mistaken beliefs and reactions to changing behaviour |
| [5. Practical human advice](#experiment-5-practical-human-advice) | 7 | Simple rules of thumb versus their reference and more elaborate policies |

Run experiment 1 first and use its development results to inform experiments
2–4. Return every resulting frozen policy to experiment 1 for performance and
robustness checks on fresh data; experiment 5 tests simplified advice. These
studies share a match-running and analysis framework, but make different
interventions. Each stage has fresh confirmation data. A finding used to choose
the next experiment is exploratory evidence, not its own confirmation.

### Shared setup and primary metrics

These rules apply unless an experiment explicitly changes them.

| Item | Specification |
| --- | --- |
| Game | Classic mode, four and five players analysed separately, default 66-point termination after a completed hand |
| Identity and information | Anonymous display names; own hand and legitimate public observations only. Any prior knowledge of opponent policies must be an explicitly labelled condition. |
| Bot lifetime | Fresh independent bot instances per match; within-match memory is permitted and recorded. No accidental learning through worker reuse. |
| Candidate identity | Freeze code version, options, composed components, memory, and resource budgets before confirmation. |
| Paired comparison | Where the design calls for a matched replacement, change one focal bot while keeping background configurations, deal sequence, seat assignment, and assigned bot seeds the same. Whole-lineup sampling is specified in experiment 1. |
| Seat balance | Rotate the whole lineup through all n seats on each deal. Randomise the initial background order across independent deals. Rotations sharing a deal remain one statistical block. |
| Data split | Separate development/pilot seeds from confirmation seeds; hold out relevant opponent combinations or variants as well. |
| Primary outcomes | Report win credit and acceptable-finish credit separately. Do not combine them into one score. |

**Win credit** is 1 for a sole winner, `1 / number_of_winners` for a tied winner,
and 0 otherwise. This is the proposed benchmark convention; also report sole
wins and shared-first finishes separately. The engine continues to recognise
all minimum-score players as winners.

**Acceptable-finish credit** is 1 for securing a qualifying picking position,
0 for missing it, and fractional when a score tie crosses the cutoff. If k
positions qualify, g players have strictly lower scores, and t players share
our score including us, credit is `max(0, min(t, k - g)) / t`. Thus two players
tied for third behind two leaders in a five-player game each receive 0.5.
This computes the agreed random tie-break exactly without adding randomness.
Identical independent policies under symmetric exposure have expected win
credit `1/n` and acceptable-finish credit `k/n`.

### Experiment 1: Strategy strength across opponent compositions

**Answers:** questions 1, 2, and the opponent-composition aspects of question 6.

**Setup.** Create a frozen reference library with the following starting entries.
The option values are proposed experimental settings, not claims of optimality.
Baseline options are `{}`; `fallback_options` and `card_options` are also `{}`.

| Family | Reference entries |
| --- | --- |
| Card order | `random`, `lowest_card`, `highest_card` |
| Board and hand | `lowest_fitting_card`, `highest_fitting_card`, `closest_gap`, `coldest_row`, `hand_flexibility` |
| Deliberate captures and bait | `controlled_burn` with `K=5`, `fallback_strategy=closest_gap`; `count_threshold_bait` with `intervening_card_threshold=3`, `candidate_ranking=most_intervening`, `fallback_strategy=closest_gap` |
| Row choice | `hand_aware_row_choice` with `max_extra_penalty=2`, card strategy `closest_gap` |

Exact option names and behaviour are defined in [Writing bots](bots.md). Keep
all eleven configurations in the study; do not filter them by an early overall
ranking. Each match selects a complete lineup, and every seat contributes a
result. Use one experiment with three complementary scheduling modes:

| Mode | How players are selected | Purpose |
| --- | --- | --- |
| Random coverage | Draw all n seats independently and uniformly from the frozen library, with replacement. Draw a fresh deal block independently of that lineup. | Covers varied tables and estimates average performance against a defined random population. |
| Controlled coverage | Schedule underrepresented compositions deliberately. Start with all-one-policy lineups and every pair of policies at every copy count; use random coverage to explore lineups containing three or more policies. | Measures homogeneous, mixed, and frequency-dependent effects that random sampling might rarely encounter. |
| Matched comparisons | For a specified opponent background C, run A+C and B+C on the same deals and seat rotations. Fill missing comparison partners for relevant or uncertain compositions. | Measures which candidate to use against the same opponents, with reduced deal imbalance. |

For controlled coverage, deduplicate identical full lineups and allocate an
initial equal number of fresh deal blocks per selected composition. Randomise
execution order. Select the initial coverage schedule before observing outcomes;
subsequent development sampling may target uncertain cells, but confirm selected
claims using a frozen schedule and fresh seeds. Every strategy remains eligible
for more coverage, including specialists with modest overall averages.

Repeated strategies use independent bot instances and private seeds. Rotate
each sampled lineup through all seats. For eleven configurations there are
1,001 distinct four-player compositions and 3,003 five-player compositions,
including repeats and ignoring seat order. Those counts are before deal repeats
and rotations. Exhaustive controlled coverage is possible if affordable;
otherwise publish the coverage achieved and leave unmeasured cells unclaimed.

Record the whole lineup, its multiplicities, sampling mode and selection
probability or planned quota, and the realised seat mapping. For each seat,
record the opponent composition by removing that seat's one instance from the
lineup. In `[A, A, B, C]`, the two A seats both face `[A, B, C]`, B faces
`[A, A, C]`, and C faces `[A, A, B]`. These are useful observations from one
match, not four independent matches. Retain seat information even when the
reported composition ignores seat order.

Drawing seats independently gives every strategy the same distribution of
opponents in expectation. Uniformly drawing from a list of distinct full
compositions is a different sampling scheme, as is requiring all players to
use different strategies. Balanced appearance counts alone do not guarantee
comparable opponent exposure. The sampling rule is part of the experiment.

Use the random stream as the initial diagnostic population; it does not claim
to reproduce the familiar human group. The controlled stream supplies extra
matchup evidence. For further population definitions, such as family-balanced
opponent frequencies or a model of the usual group, declare the weights and
check coverage before estimating a new average. Adding candidates should not
silently change an existing benchmark: evaluate newcomers against the frozen
opponent population, or publish a separately versioned expanded population.
Simulation and LLM candidates need explicit configurations and resource budgets.

Robustness is a set of conditions within this experiment, not a separate study.
Include the following named conditions alongside the initial population:

| Condition | Setup |
| --- | --- |
| Different population weights | Compare uniformly sampled entries with sampling a family uniformly and then an entry within it. Keep these population summaries separate. |
| Different group subsets | Use a reference roster of `lowest_fitting_card`, `highest_fitting_card`, `closest_gap`, `coldest_row`, controlled burn, and hand-aware row choice. Compare focal candidates against all three-opponent subsets for four-player games and four-opponent subsets for five-player games. These entries represent behaviours, not specific people. |
| A tactic becomes common | Use the controlled copy-count lineups, including lineups with several independent copies of a new combined policy when it becomes available. |
| Opponents mix or adapt their tactics | Add frozen switching and counterplay policies developed in experiments 3–4 as named opponent conditions. Preserve the original benchmark population rather than silently expanding its weights. |
| Different seat orders | Repeat selected compositions with additional permutations, including reversed background order, on matched deals. |

For a new focal candidate absent from the original library, sample the n−1
opponents under the chosen condition, then insert that candidate and each
reference in matched focal slots. This evaluates new policies against common
opponent exposure without requiring them to occur in the original random stream.
Reuse this experiment after later stages produce new policies, with fresh
confirmation seeds. It is a continuing benchmark, not a one-off opening round.

**Metrics.** For each strategy, player count, and opponent composition: win
credit, acceptable-finish credit, finishing distribution, and independent block
count. Also measure paired candidate differences against common backgrounds,
changes in those differences between compositions, and performance as the number
of copies of a strategy changes. Measure deterioration under changed conditions
and the weakest tested compositions with their coverage and uncertainty.
Report population-average credits separately, alongside completion and resource use.

**How results are measured.** Produce three views of the same study:

1. A conditional performance table, with strategies as rows and opponent
   compositions as columns, separately for each objective and player count.
   Show estimates, uncertainty, and coverage in each cell. Compare strategies
   within a common column; use the matched-comparison mode to resolve important
   differences. A comparison between A and B in the same match is instead
   co-play evidence: they face different opponent compositions.
2. Population summaries under explicitly named opponent distributions. For the
   IID random stream, average each strategy's seat credits over its finished
   appearances in that stream. If matches fail, this point estimate is conditional
   on completion; report coverage and missing-outcome bounds alongside it.
   With complete outcomes it estimates the population average without observing
   every possible composition. Controlled extras must not enter that raw
   average: their quotas would change the population being measured. They may
   contribute to a separately weighted estimate from conditional results when
   the required coverage supports it. Do not treat unobserved cells as zero or
   silently renormalise the target population onto the cells observed.
3. A robustness view showing changes in each strategy's performance and its
   advantage over a reference across populations, group subsets, tactic
   frequencies, and seat orders. Report weak cells, not just the mean. Recheck
   apparent vulnerabilities on fresh data; the noisiest minimum is not
   necessarily the true weakest condition. Distinguish a useful specialist from
   a policy that remains competitive across the tested conditions.

Use whole deal/lineup blocks for uncertainty, retaining all seats, rotations,
and related replacement matches. Sparse conditional cells need more data even
when the population average is precise. If a lineup is sampled once and then
repeated on many deals, account for that shared lineup draw when generalising
to a population; those deals improve that lineup's estimate, not population
coverage. A fixed controlled schedule instead conditions on its declared cells.

Rank reversals across compositions and changing advantages reveal specialists
and possible counters. An increase in raw wins against easier opponents alone
does not establish specialisation. Identical self-play copies have symmetric
expected credits, so self-play wins cannot rank their strategies. Keep
composition-specific recommendations alongside any overall ranking; no result
establishes a universal best policy or universal counter.

### Experiment 2: When a tactic helps

**Answers:** question 3.

**Setup.** Begin with three controlled interventions:

| Idea | Treatment at the chosen decision | Reference |
| --- | --- | --- |
| Cheap deliberate capture | Controlled-burn selection with `K` separately set to 3, 5, or 8 | `closest_gap` card selection |
| Bait into a full row | Count-threshold bait with threshold separately 1, 3, or 5, ranking `most_intervening` | `closest_gap` card selection |
| Hand-aware row selection | Hand-aware row choice with extra allowance separately 1, 2, or 3 | Cheapest-row choice, retaining the same `closest_gap` card policy |

Generate source matches with `closest_gap` as the focal player and backgrounds
drawn independently from experiment 1's uniform-entry reference population. For each tactic, use a separate sampling
seed to choose uniformly among eligible focal decisions in each source match:
a below-all card opportunity, a full-row candidate, or a required row choice,
respectively. Record zero-opportunity matches too. Do not select situations by
their eventual outcome. Record the hand, board, legal public history, current
scores, and remaining plays.

Rerun the same prefix in paired arms, then enable the tactic or take the reference
action at that single chosen decision. Afterwards the focal player returns to
`closest_gap` and the opponents keep their source policies through match end;
opponents react normally to the changed
board. The arena can use the true simulated state to keep the arms matched,
but the bots receive only legal observations. Start with the replayable heuristic
and composed policies above. An engine snapshot alone cannot restore a learning
bot's private memory; experiments using those bots must reproduce that too.

Define condition groups before confirmation: for example cheapest-row cost
1–3, 4–5, or 6+, early/middle/late hand, and whether the player is currently
inside the target positions, tied at the boundary, or outside them. Use the
same pre-intervention conditions in both arms. Record eligible opportunities,
including those where treatment and reference happen to choose the same action.

**Metrics.** Paired change in win and acceptable-finish credit; penalties taken
on the current play and through the rest of the hand; opportunity frequency;
frequency with which the tactic actually changes the action.

**How results are measured.** For each tactic and condition group, report the
paired final-outcome effect, interval, and number of independent source matches.
The estimate gives each source match with an eligible decision equal weight;
it is not weighted toward matches with many opportunities. Conditions describe
states reached under the source policy, with broader transfer tested in
experiments 3 and 5. Cluster related interventions by source match. A cheaper immediate
play is not sufficient evidence of a better match decision. Discover candidate
conditions on development data and confirm those conditions on new source
matches; report rare conditions as uncertain when appropriate. The output is
a table of situations in which a one-time tactic helps, hurts, or remains
unresolved. Following that tactic for the whole game is tested next.

### Experiment 3: Combining and switching tactics

**Answers:** questions 1 and 4.

**Setup.** Use the components and confirmed conditions from experiment 2 to
construct candidate policies. Start with simple decision lists of at most three
if/then rules, using only own-hand features, public board/history, remaining
plays, and current scores. Select rule thresholds on development data, then
freeze the rules. Develop one selector for win credit and another for
acceptable-finish credit; evaluate both under both objectives.

This experiment selects tactics from game-state features without an explicit
inferred opponent profile. Experiment 4 tests the added value of such profiles
and their updating, using an otherwise comparable base selector.

Compare five alternatives on experiment 1's reference population and its
informative opponent compositions:

- Each fixed component policy, including `closest_gap`.
- The existing composed policy that applies its tactic whenever its trigger holds.
- The situation-dependent selector.
- A version of that selector with score/rank information removed.
- A random selector using the same component frequencies measured on development
  data, but choosing independently of the current situation.

For each candidate, record exactly which component acts, the switching rule,
row policy, and any memory. Fixed and random controls have the same available
components. Run whole matches; this experiment measures the consequences of
repeated switching, not isolated actions. New selectors are proposed bot work,
not a capability the current registry already supplies automatically.

**Metrics.** Both primary credits; paired gain over the fixed reference and the
best fixed component selected on development data; gain over random switching;
change after removing score awareness; switch frequency and decision cost.

**How results are measured.** Produce a comparison table separately for each
objective and player count. Confirm the planned paired differences using new
blocks. Improvement over a fixed component shows the value of the complete
combined policy; improvement over the random control adds evidence that its
choice of when to switch matters. Compare the two goal-specific selectors
across both outcomes to expose trade-offs. Do not claim a useful rule from its
frequency of use alone, or choose the "best fixed" comparator after seeing the
confirmation outcomes.

### Experiment 4: Recognition and counterplay

**Answers:** question 5 and the belief/adaptation aspects of question 6.

**Setup.** Select candidate counters from the development results of experiments
1–3 and freeze the mapping from inferred behaviour to response. Test against
pure reference policies, held-out parameter variants, and switching opponents.
Include a defined phase-switching opponent that uses `highest_fitting_card` for
plays 1–5 and `lowest_fitting_card` for plays 6–10, plus hand-dependent composed
opponents. Fix their row rules and configurations in the experiment plan.

Compare three versions of the same focal player:

1. A frozen game-state selector from experiment 3 with opponent inference disabled
   (or `closest_gap` if no selector improves on it in development).
2. The same base player with opponent inference and a counter chosen from its
   own hand and observed public play.
3. An informed version given the opponent's policy definition, including switching
   rules, but no hidden hand, unrevealed card, or private current mode.

For the evidence-based version, separately allow the detector the latest 0,
10, or 30 public card-reveal plays: one play means a complete simultaneous
card-selection/reveal turn, with ten plays per hand. The detector must not
retain excluded history elsewhere. Other policy settings stay fixed. Evaluate competitive
benefit in full 66-point matches; use a separate five-hand diagnostic run to
compare recognition after equal exposure, without selecting only matches that
survive long enough. Record predictions before the next card is revealed.

Include three further conditions to measure the reliability of recognition
and response:

| Condition | Setup |
| --- | --- |
| Wrong initial belief | Give one opponent's wrong profile initial probability 0.9 and distribute the remaining 0.1 uniformly among alternatives. Compare with the correct profile at the same confidence and with a uniform prior. Freeze the wrong-label mapping and allow observed evidence to update it. |
| A persistent change of approach | The opponent uses `highest_fitting_card` in hands 1–2 and `lowest_fitting_card` from hand 3, with cheapest-row choice throughout; also test the reverse transition. Include five-hand diagnostic runs so every subject encounters the change. |
| Opponents respond to us | Give opponents a frozen public-history counter-selector developed on separate data. Freeze focal and opponent versions before confirmation; they adapt independently within a match, without shared memory or coordination. |

These tests measure within-match learning and recovery, plus the use of declared
prior beliefs. They do not establish learning about familiar people across
separate games: fresh bot instances still reset at every match. Experiment 1
also uses these frozen opponent policies as named benchmark conditions, while
this experiment isolates the contribution of recognition and belief updating.

**Metrics.** Primary-credit gain over no counterplay; difference from the informed
reference; correctness and coverage of announced behaviour labels in controlled
cases; wrong counter switches; plays required to detect the known phase change;
next-card prediction accuracy, deterioration caused by a wrong prior, recovery
after a behaviour change, and resource cost. Allow "insufficient evidence"
and report how often it occurs. Different tactics can explain the same observed
card, so label accuracy is diagnostic rather than the ultimate objective.

**How results are measured.** Report counterplay gains with paired intervals by
opponent type and available history. Use complete match blocks for outcome
effects and complete fixed-length diagnostic matches for detection measures.
Report undetected changes as undetected, not zero delay. A detector is useful
when its resulting decisions improve the relevant objective, even if exact
policy labels are imperfect. For pure and phase-switching opponents, compare
announced labels with the instrumented active card policy. Coverage is the
fraction of predictions that do not abstain. Next-card accuracy is the fraction
of forecast opportunities where the predicted card matches the next revealed
card; an abstention counts as a miss. A wrong counter switch selects a
response different from the frozen mapping for that active policy, not merely
an action followed by a poor result. For ambiguous mixed behaviours, report
prediction and outcome measures without forcing a single correct label.

Detection delay counts completed post-change plays before the first correct,
non-abstaining announcement of the new policy; an announcement before its first
changed play has delay zero. Restart the delay clock at each hand's phase
change, without resetting permitted learned information, and record detection
failures before the next reset. For the hand-3 change, start the clock at that
change and retain non-detections through the fixed-hand endpoint. Compare
outcome losses from wrong beliefs and gains from updating with paired intervals;
a confident initial error need not be recoverable within a normal match.
The informed condition measures the value of
additional policy knowledge within the tested system; it is not an optimal
player or a proven upper bound. The output should state which observable
patterns warrant which responses and how much evidence those responses need.

### Experiment 5: Practical human advice

**Answers:** question 7, drawing on questions 3–6.

**Setup.** Select up to three promising rules from the confirmed tactical and
counterplay findings. Write each as: observable trigger, recommended action,
intended objective, and exceptions. For example, a candidate rule might specify
when a cheap deliberate capture is worthwhile; its threshold must come from
the earlier experiment, not be assumed beneficial in advance.

Create three versions: the reference policy without the rule, a simple
implementation of the rule, and the more elaborate policy that motivated it.
Test each rule separately, then the combined advice with an explicit priority
when rules conflict. Freeze all choices before running held-out full matches
against ordinary, mixed, and robustness backgrounds. The simple version may use
only information and calculations a human could realistically apply.

Follow simulation with a small prospective study in the usual group: record
which advice players know, whether they recognise applicable situations, what
action they choose, and the subsequent outcomes. Record roster and experience.
Learning advice cannot be undone, so alternating "with advice" and "without
advice" after instruction is not a clean controlled comparison. A causal human
study would need independent groups with randomised or delayed instruction;
a small follow-up in one familiar group should be described as exploratory.

**Metrics.** Primary-credit gain over the no-rule reference; performance lost
relative to the elaborate version; applicability frequency; interaction between
rules; required observation/calculation effort. In human follow-up, measure
correct trigger recognition, adherence, misunderstandings, and descriptive match
outcomes rather than assuming correct execution.

**How results are measured.** Use paired held-out simulation effects and their
intervals to decide whether simplification retains a useful benefit. For a
controlled human study, cluster by independent instruction/group assignment;
repeated matches with the same learning players are not independent. Keep the
single usual-group follow-up descriptive unless a longitudinal analysis
explicitly accounts for repeated players and accumulated learning.
Produce an advice sheet giving each rule's goal, trigger, action, exceptions,
effect estimate, and scope of evidence. Distinguish "works when implemented as
a bot" from "people can use it successfully". Advice that is hard to recognise
or execute should be revised and evaluated on new data.

## 3. How comparisons are measured

**Random-lineup estimates.** In the IID stream, divide each strategy's total
finished-seat credit by its finished appearances. This point estimate is
conditional on completion when outcomes are missing; always show planned
appearances, completion coverage, and missing-outcome bounds. Resample whole independent lineup/deal
blocks and recompute these ratios, and any differences between strategies, in
each resample. Keep absent strategies absent; do not assign them zero scores or
restrict the analysis to matches where both compared strategies appeared.
Either would change the comparison. Multiple copies and seat rotations stay
together. Controlled sampling records support condition-specific estimates,
not the unweighted IID population average.

**Paired differences.** On each matched deal, subtract the reference bot's
outcome from the candidate's outcome, then average these paired differences
across blocks. Report differences in percentage points and a 95% confidence
interval obtained by resampling whole independent blocks. Keep both candidates,
all rotations, and any related state interventions together in each resample.
For weighted opponent conditions, recompute the declared weighted mean in each
resample; extra sampling must not change the weights. For fixed controlled
quotas, resample independent blocks within each predefined condition, preserving
cross-condition dependencies where deals are shared. Do not let a resample drop
a condition and silently change the target weights. Report independent block
counts as well as the larger number of matches. Individual cards, hands within
a match, and player results from the same match are not independent samples.

**Sample size and decisions.** Start with 100 IID random pilot blocks per player
count for the cheap-bot population study. Controlled conditions and matched
contrasts need their own pilots. Use block-resampled precision estimates or
prospective simulation for the random-lineup ratios, and observed variability
for matched contrasts, together with runtime to fix confirmation budgets. A proposed precision
target is a 95% interval half-width of two percentage points for each primary
comparison. An approximate planning count is `(1.96 * s / 0.02)^2`, where s is
the pilot standard deviation of matched block differences measured on the
0–1 credit scale; this shortcut applies to matched comparisons, not ratios
with a changing number of strategy appearances.
Rare tactical triggers and expensive bots need their own pilots. Do not stop
an ordinary fixed-sample experiment when significance first appears.

Use two percentage points as a proposed minimum useful improvement. A confidence
interval entirely above that threshold supports a practically useful gain;
one entirely within ±2 points supports practical equivalence for that comparison.
Otherwise report the estimated direction and unresolved uncertainty. Prespecify
primary contrasts and account for testing multiple claims; exploratory rankings
and selected subgroup findings need fresh confirmation. Precision defaults are
planning choices, not a guarantee that 100 games, or any fixed number, suffices.
Reporting intervals alongside point estimates follows the general motivation in
[Agarwal et al.'s evaluation study](https://arxiv.org/abs/2108.13264); the block
definition here follows this arena's experimental dependencies.

**Supporting measurements.** In every experiment retain final scores, complete
finishing orders including ties, completed-hand scores, outcome status and
responsible seat, attempts/rejections, and available decision time and resource
use. Report completion rates alongside competitive outcomes. Failed or unfinished
matches do not have competitive scores; do not count partial penalties as a
successful low score or silently replace failed attempts. Show missing-data
sensitivity: with observed credit T, N planned appearances, and M missing
outcomes, the possible mean credit lies between `T/N` and `(T+M)/N`. In paired
analyses also report complete-pair coverage and bound missing differences.

Penalties explain behaviour but do not replace either primary outcome. Match
length depends on who reaches 66; neither final penalties nor penalties divided
by variable match length provides a clean equal-exposure strategy comparison.
Use fixed-hand studies only where explicitly specified in an experiment. Avoiding costly
hands, finishing second on average, and avoiding last place are also different
from reaching the agreed acceptable positions.

## 4. Arena support required by these experiments

The routine catalogue comparison workflow is now available through `arena`,
with planning, compact outcomes, population and paired analytics, and saved-data
reanalysis. See [Comparing strategies](comparisons.md) for the implemented
interface. The audit below records the original starting point and proposed
capabilities; tactical interventions and recognition workflows remain future
work.

The rules engine already supplies the correct foundation: legal observations,
deterministic deals, action validation, scoring, and both fixed-hand and
66-point termination. Experiment scheduling and measurement belong above it.
The [arena guide](arena.md), [rules guide](game-rules.md), and
[trace guide](traces.md) describe the existing interfaces.

| Experiments | Implemented foundation | Further work |
| --- | --- | --- |
| All | One `RunSettings`/`application.run` workflow, versioned plans, compact per-match evidence, declared objectives/cutoffs, and optional traces | Additional question- and intervention-specific provenance as required below |
| 1 | Random/controlled lineups, replacement arms, rotations, copy-count coverage, sampling probabilities, quotas, and incomplete-job accounting | Freeze the chosen evaluation design before collecting confirmation evidence |
| All | Both competitive credits, finishing distributions, failure coverage, population weights, paired differences, and block intervals | Select the evidence and practical-effect thresholds for each stated question |
| 2 | Deterministic matches can be rerun with instrumented policies | A controlled one-decision intervention wrapper, trigger sampling, and source-match IDs; preserve or reconstruct bot state when needed |
| 3–4 | Bots can maintain state and implement composed behaviour | The specified switching policies, detector/counter variants, history controls, and optional per-decision diagnostic records |
| 5 | Simple rules can be implemented as bots and compared using the same match runner | An advice record linking each rule to its supporting experiment and a separate human-observation protocol |

Use stable configuration identities across seats, including implementation
revision, validated options and nested components. Preserve code/lockfile hashes,
Python/runtime, seed scheme, hardware, concurrency, and any provider/prompt
versions. Fixed seeds do not guarantee identical provider responses, numerical
results after dependency upgrades, or timeout behaviour. Full traces are optional
for large sweeps, but compact result records must retain the data needed to
recompute both primary metrics and all comparison denominators.

There are several implementation details that matter to this plan:

- [Compact evidence](../src/sixnimmt/arena/artifacts.py) retains finished scores,
  winner indices, and completed-hand scores separately from unfinished partial
  scores. Derive competitive denominators from finished outcomes and completed
  hands, preserving missing-game coverage.
- [Seed derivation](../src/sixnimmt/arena/planning.py) keeps match and bot streams
  separate, deriving both from one root. Each job records private bot seeds and
  preserves their assignments across prescribed replacement arms. Changing
  player count changes dealt hands and starting rows, so equal seeds across four
  and five players do not identify the same dealt state.
- [Per-match analytics](../src/sixnimmt/analytics/summary.py) and
  [LLM statistics](../src/sixnimmt/arena/bots/llm.py) already expose useful action,
  latency, token, repair, and model metadata. Standardise those measurements;
  missing usage is not zero cost. Count batch latency once per bot call, and
  include all calls needed to select/commit a card, not just the final call.
- Timeouts apply to individual bot calls and cannot forcibly stop arbitrary
  in-process work. Hold execution settings fixed within comparisons and report
  unfinished calls and cost alongside outcomes. The current `reproducible`
  flag is a bot determinism declaration, not an environmental guarantee.
- The existing [probabilistic bots](uncertainty-bots.md) learn within a match,
  model a limited policy catalogue, and optimise own penalties over a horizon
  capped at the current hand. Their risk settings do not directly optimise
  winning or qualifying. Larger inference budgets and held-out opponents are
  needed to distinguish model assumptions from sampling problems; the
  [MCMC study](../experiments/mcmc/REPORT.md) documents difficult mixing cases.

Implement the plan/export and analysis support first, so experiment 1 can
run reproducibly. Add the intervention and switching/recognition bot work as
experiments 2–4 require it. Keep changes to gameplay rules separate from these
measurement features, preserve existing seed vectors and trace compatibility,
and test ties, seat balance, pairing, unfinished denominators, and deterministic
backend equivalence. Run the default suite and relevant volume tests for future
scheduling, determinism, or information-boundary changes, following
[Contributing](../CONTRIBUTING.md).

Each experiment's final output should include its frozen specification, candidate
configurations, data split, counts of planned/completed independent blocks and
matches, metrics and paired intervals, failure/resource results, and conclusions
limited to the conditions tested. This makes the path from an agreed question
to an experimental result—and eventually to playing advice—explicit.
