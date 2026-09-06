# GPT 5.6 arena comparison: gameplay, decisions, memory, and efficiency

**Run:** `arena_20260906T173132Z_6769`

**Analysed:** 6 September 2026

**Scope:** the 16 matches in this directory, comparing Luna, Terra, and Sol

**Primary source:** [manifest.json](manifest.json), cross-checked against all event, action, and model logs

The raw traces and manifest are stored in [traces.zip](traces.zip) through Git LFS. To restore the files referenced by this report, run these commands from the repository root after cloning with Git LFS installed:

```bash
git lfs pull --include="experiments/gpt-5.6-comparison/traces.zip"
unzip experiments/gpt-5.6-comparison/traces.zip -d experiments/gpt-5.6-comparison
```

The extracted JSONL files and manifest are ignored by Git. Their links in this report work after extraction.

## Executive findings

Terra produced the strongest aggregate results, but this run does not establish that it consistently makes the best decisions. Its advantage over Sol is reasonably consistent; its advantage over Luna remains uncertain with only 16 games.

1. **Terra's advantage comes mainly from fewer sixth-card penalties, not fewer captures overall.** Terra and Luna capture almost equally often, but Terra pays less per capture. Sol has more costly sixth-card captures and a heavier tail of bad hands.
2. **The models order their cards differently.** Sol chooses its smallest remaining card much more often. Terra spends high cards more often and sometimes preserves a lower card's alternative placement route. These are observable styles, not inferred personalities.
3. **Winning does not imply sound decision-making.** Terra overlooks a guaranteed end-of-hand win in game 5, then eventually wins that match by one point. Luna makes clear cost-ranking mistakes. Sol always chooses a minimum-cost row when asked, despite having the weakest aggregate score.
4. **Simultaneous selection creates risks that static row calculations miss.** A below-all opponent card can remove a short destination row and redirect another card into a full row. The traces contain both sound anticipation and incorrect certainty.
5. **Only Terra uses the optional notebook.** Its 232 accepted updates include 64 memory-only calls, all followed by unchanged game observations. Some notes support correction; others duplicate observations, preserve errors, or describe unperformed actions as completed.

All analysis was local. No additional games or paid model requests were made, and no game or bot implementation was changed. The bootstrap and offline simulations below reuse recorded data and ordinary computation; they are not additional model evaluations.

## 1. Run and implementation audit

### Configuration and completeness

| Item | Verified configuration or result |
|---|---|
| Games | All 16 completed: `arena_0` through `arena_15` |
| Exposure | 81 hands; 810 card selections per model; matches lasted four to six hands |
| Event logs | 17,182 events; every match ends with `match_ended`; event sequences are continuous |
| Action logs | 2,955 accepted action records, including memory; action sequences are continuous |
| Model logs | 2,787 requests, each with a response and parsed action/batch record |
| Incomplete logs | No malformed or incomplete JSONL lines, missing request/response/parse chains, or pending transaction files found |
| Fixed seats | `player_1`: Luna; `player_2`: Terra; `player_3`: Sol, in every game |
| Actual model identifiers | `openai/gpt-5.6-luna`, `openai/gpt-5.6-terra`, `openai/gpt-5.6-sol`; recorded response model identifiers agree |
| Rules | 104 unique cards; ten cards per player; four rows initially containing one card each; capacity five |
| Ending | Finish the hand when anyone reaches at least 66 banked penalties; lowest score wins; ties share victory |
| Communication | Disabled; selecting a card immediately commits it |
| Prompts | Same system prompt; empty strategy prompt; prompt version 7, observation version 3 |
| Model settings | `max_tokens=2048`, `tool_choice=required`; temperature omitted; no explicit reasoning-effort setting or provider-option override |
| Tool schemas | Same structure across models; legal card enums vary with each hand; non-strict and unsimplified; parallel tool calls not explicitly disabled |
| Memory | Optional replacement notebook, version 2, at most 4,000 characters; persists within a match and resets between matches |
| Scheduling | Sequential decisions within a match; four matches may run concurrently |
| Action limits | One to eight calls per response; at most one memory update; match limit 10,000; no per-play limit |
| Time and recovery limits | 60-second provider timeout; 120-second bot decision budget; one parse-repair attempt; rejection limit eight; no separate arena decision timeout |
| Errors and recovery | Zero rejected actions, parse repairs, provider failures, timeouts, or abandoned decisions |
| Completion limits | Every response finishes with tool calls; largest recorded completion is 1,381 tokens, below the configured 2,048 limit |
| Seeds and rotation | Run seed 6769; 16 distinct derived match seeds; all 81 deals reproduced from their seeds; no repeated deals or seat rotation |

The directory contains one unambiguous run. The manifest's completion counts agree with the logs. The manifest marks the run non-reproducible because model decisions are nondeterministic; this does not prevent reproducing the recorded deals or analysing the recorded actions.

### What the models actually saw

The [prompt and observation implementation](../../src/sixnimmt_server/arena/bots/prompt.py) supplies:

- the player's sorted remaining hand;
- each row's cards, occupancy, and penalty total;
- banked and this-hand scores, plus opponents' remaining card counts;
- current-hand revealed/captured cards outside the table;
- up to 20 recent public plays, including already-revealed pending cards during row choices;
- the private notebook, quoted as potentially stale data.

Each request is constructed afresh from system instructions and that observation. It does not carry the previous model conversation. Recent public history can include previous hands, but current-hand card exclusions are separately identified. With at most ten plays per hand, the 20-play history bound is sufficient to retain the current hand's public plays.

The [rules engine](../../src/sixnimmt_server/engine/resolution.py) chooses the row whose last card is the largest value below the selected card. A sixth card captures the existing five and starts a replacement row. A card below all row ends requires a row choice. Penalties are 7 for 55, 5 for other multiples of 11, 3 for multiples of 10, 2 for other multiples of 5, and 1 otherwise. The replacement card is not captured.

Communication was disabled, so messages, bluffing, promises, and coordination cannot be evaluated. Opponents' model display names were visible. Although the scheduler offers seats sequentially, hidden card values are not exposed in the classic-mode observation. There is no evidence here of a scheduling information leak.

### Acceptance and reconstruction

The analysis distinguishes four stages: the supplied observation, the proposed response, the accepted transaction, and subsequent resolution.

The [transaction implementation](../../src/sixnimmt_server/arena/transactions.py) preflights the entire batch. A rejected batch would apply neither game actions nor memory. The [runner](../../src/sixnimmt_server/arena/runner.py) records acceptance and applies notebook updates through the [memory bot](../../src/sixnimmt_server/arena/bots/llm_memory.py).

Every model request was paired with its response and parsed record by `request_id`, then joined to its action transaction using chronological order, player identity, the offered view, and decision timing. Proposed tool types matched the accepted action records. Selected card values and row indices matched their game events. Every supplied observation was independently reconstructed using the repository's player-view event folder and matched the recorded observation text.

Notebook reconstruction began empty for each player/match and changed only on accepted updates. Every subsequent supplied notebook matched that reconstructed value. No conclusion about acceptance relies solely on a parsed response.

### Comparison confounds

The configured prompts, schemas, and memory allowances are equivalent. Nevertheless:

- Each model stays in one seat and receives different cards. No deal is replayed with models swapped.
- Provider defaults, including reasoning effort when unspecified, are not experimentally controlled.
- Optional memory is used only by Terra. The results compare these model-and-workflow combinations, not isolated model capability with equal memory behaviour.
- Match length depends on performance. Later-hand exposure and final scores are affected by the stopping rule.
- Concurrent requests may affect latency through provider load. The run does not isolate intrinsic model inference time.

These limitations qualify model comparisons without invalidating the recorded observations and outcomes.

## 2. Results and uncertainty

### Per-model performance

Lower scores are better. A sole winner receives one win. A tied minimum would be counted separately as a shared win, without fractional allocation. No shared wins occurred.

| Metric | Luna | Terra | Sol |
|---|---:|---:|---:|
| Sole wins / 16 | 4 | **9** | 3 |
| Shared wins | 0 | 0 | 0 |
| Final score, mean | 61.19 | **51.88** | 66.75 |
| Final score, median | 61 | **51** | 66.5 |
| Final score, sample standard deviation | 11.55 | 19.39 | 12.32 |
| Final score, range | 44–84 | 25–81 | 49–98 |
| Total penalties | 979 | **830** | 1,068 |
| Penalties per hand, pooled over 81 hands | 12.09 | **10.25** | 13.19 |
| Penalties per card play, over 810 selections | 1.209 | **1.025** | 1.319 |
| Hand penalties, median / worst | 12 / 30 | **9 / 28** | 12 / 44 |
| Hand penalties, sample standard deviation | 6.90 | 7.25 | 8.68 |
| Captures / 810 plays | 183 | 184 | 192 |
| Mean points per capture | 5.35 | **4.51** | 5.56 |
| Captures worth at least 10 / at least 15 | 28 / 3 | **24 / 3** | 36 / 7 |
| Largest capture | 16 | 15 | 17 |
| Sixth-card captures: count / points | 89 / 762 | **69 / 583** | 108 / 934 |
| Below-all captures: count / points | 94 / 217 | 115 / 247 | 84 / 134 |

Final scores and pooled hand rates answer different questions. Final scores describe results under the target-score stopping rule. Pooled rates weight longer matches more heavily. Giving each game equal weight when calculating penalties per hand instead produces Luna 12.18, Terra 10.10, and Sol 13.43, preserving the ordering.

### Per-game distribution

`G0` means `arena_0`, and similarly for the other indices. Each linked game is its event log. Adjacent files with `.actions.jsonl` and `.model.jsonl` contain action acceptance and model requests/responses.

| Game | Hands | Luna | Terra | Sol | Winner | Match seed |
|---|---:|---:|---:|---:|---|---:|
| [G0](arena_20260906T173132Z_6769_0.jsonl) | 6 | 65 | 76 | **57** | Sol | 11092062191947415880 |
| [G1](arena_20260906T173132Z_6769_1.jsonl) | 6 | 71 | 81 | **55** | Sol | 11777131628951565655 |
| [G2](arena_20260906T173132Z_6769_2.jsonl) | 5 | 72 | **30** | 65 | Terra | 17720259270208665764 |
| [G3](arena_20260906T173132Z_6769_3.jsonl) | 5 | 55 | 76 | **54** | Sol | 6054463493841611099 |
| [G4](arena_20260906T173132Z_6769_4.jsonl) | 5 | **44** | 57 | 76 | Luna | 3477678155326780609 |
| [G5](arena_20260906T173132Z_6769_5.jsonl) | 5 | 58 | **57** | 70 | Terra | 12149454711385337954 |
| [G6](arena_20260906T173132Z_6769_6.jsonl) | 5 | 67 | **36** | 63 | Terra | 16329038322484455152 |
| [G7](arena_20260906T173132Z_6769_7.jsonl) | 5 | 66 | **36** | 59 | Terra | 16730598566881874307 |
| [G8](arena_20260906T173132Z_6769_8.jsonl) | 4 | 59 | **39** | 75 | Terra | 10931186153524279499 |
| [G9](arena_20260906T173132Z_6769_9.jsonl) | 5 | 84 | **25** | 55 | Terra | 11203563276950316001 |
| [G10](arena_20260906T173132Z_6769_10.jsonl) | 5 | 63 | **45** | 98 | Terra | 14792114803745396990 |
| [G11](arena_20260906T173132Z_6769_11.jsonl) | 5 | **46** | 64 | 77 | Luna | 10610238517657987099 |
| [G12](arena_20260906T173132Z_6769_12.jsonl) | 5 | 77 | **39** | 49 | Terra | 18399208800828710456 |
| [G13](arena_20260906T173132Z_6769_13.jsonl) | 4 | 50 | **29** | 71 | Terra | 4350568993785869977 |
| [G14](arena_20260906T173132Z_6769_14.jsonl) | 5 | **47** | 61 | 76 | Luna | 16463534987714209130 |
| [G15](arena_20260906T173132Z_6769_15.jsonl) | 6 | **55** | 79 | 68 | Luna | 1894441556709807868 |

### Finding 1: Terra's advantage is more consistent against Sol than against Luna

**Descriptive result.** Terra finishes below Sol in 12/16 games and below Luna in 9/16. Luna and Sol split their head-to-head comparison 8–8, despite Luna's better average score. Terra's final scores are more variable than either opponent's: it is strong in many games rather than uniformly strong.

Paired final-score differences give the following results. Positive values favour the first model named.

| Comparison | Mean advantage, points | Exploratory 95% bootstrap interval | Mean advantage after removing any one game |
|---|---:|---:|---:|
| Terra over Luna | 9.31 | −2.69 to 22.06 | 6.00–11.53 |
| Terra over Sol | 14.88 | 3.50–25.88 | 12.33–17.60 |
| Luna over Sol | 5.56 | −4.44 to 15.31 | 3.60–7.87 |

The average ordering survives every leave-one-game-out check. However, the Terra–Luna interval includes no advantage. The intervals are exploratory and not adjusted for examining multiple comparisons.

**Exploratory catastrophe check.** Sol has four hands costing at least 30 points, compared with Luna's one and Terra's zero. At a threshold of 25 points, the counts are 8/81 for Sol and 4/81 for each other model. Sol's worst is G10 H5: 44 points from captures of 1, 12, 9, 15, and 7. Removing all of G10 still leaves Terra ahead of Sol by 12.33 points per game. A catastrophic hand amplifies the difference but does not create it. See [G10 events](arena_20260906T173132Z_6769_10.jsonl), hand 5.

| Model | Worst hands: game / hand / penalties, including ties |
|---|---|
| Luna | G3 / H2 / 30; G9 / H1 / 27; G6 / H3 / 26 |
| Terra | G3 / H1 / 28; G0 / H3 / 25; G4 / H2 / 25; G15 / H4 / 25 are tied behind the worst |
| Sol | G10 / H5 / 44; G2 / H3 / 38; G10 / H3 / 30 and G13 / H3 / 30 are tied next |

**Interpretation.** Terra's advantage is not just one favourable result, but 16 unmatched games are insufficient to establish general superiority. Its lower sixth-card exposure is a plausible contributor, not an isolated causal explanation.

**Follow-up.** Replay matched deals across all six seat permutations, using both fixed-hand and 66-point endpoints. Measure paired penalties, win probability, and catastrophic-hand frequency. This requires more games but separates seat/deal effects and stopping effects from model behaviour.

### What “20,000 bootstrap resamples of whole games” means

**No new games were played. No model requests were made. There are still only 16 observed games.**

For a comparison such as Terra versus Luna, each existing game supplies one score difference. In G0, Luna scored 65 and Terra scored 76, giving Terra an advantage of −11 points.

The local calculation repeatedly:

1. Picks 16 game indices from the existing 16, allowing repeats. A sample might contain G0 twice, G4 three times, and omit G9 entirely.
2. Calculates the mean score difference for that sample.
3. Repeats the process 20,000 times using Python arithmetic.

The 2.5th and 97.5th percentiles of those averages form the reported interval. A “whole game” stays together: Terra's score is compared with Luna's score from that same game. Individual plays are not treated as independent evidence.

This estimates sensitivity to which games happened to be observed. Increasing the number of resamples makes the numerical estimate smoother; it does **not** increase the underlying sample size or supply evidence about unseen game types. The method cannot correct fixed-seat effects, nonrepresentative deals, or model settings. With 16 games, these intervals should be treated as rough uncertainty estimates.

## 3. Gameplay and decision quality

### Finding 2: Card ordering differs more clearly than total capture frequency

**Descriptive result.** Terra and Luna capture almost equally often—184 versus 183 times—but Terra's captures cost less. Terra incurs 179 fewer sixth-card points than Luna and 351 fewer than Sol, while paying somewhat more through below-all choices.

For the 729 non-forced selections per model:

| Selection feature | Luna | Terra | Sol |
|---|---:|---:|---:|
| Smallest remaining card | 357, 49.0% | 340, 46.6% | **432, 59.3%** |
| Largest remaining card | 127, 17.4% | **177, 24.3%** | 122, 16.7% |
| Mean normalized rank in remaining hand | 0.313 | **0.381** | 0.276 |
| Median gap above current destination tail, excluding below-all cards | 7 | 10 | 7.5 |
| Current destination has one or two cards, excluding below-all selections | 398/633, 62.9% | 357/592, 60.3% | 328/610, 53.8% |
| Selected into a currently full destination row | 63/729, 8.6% | 59/729, 8.1% | 70/729, 9.6% |

Normalized rank is `(ascending rank − 1) / (remaining hand size − 1)`: zero is smallest, one is largest. These are features of the table at selection time, not predictions of the eventual destination.

Sol's smallest-card frequency exceeds Terra's in 15/16 games. Sol also selects below-all cards earlier: 74/243 selections in plays 1–3, versus Terra's 46 and Luna's 29. “Sol retains low cards too long” is therefore not supported as a general explanation. Sol has only two final-play row choices, compared with Terra's 13 and Luna's 17.

**Preserving options: a positive example.** In G7 H1 P7, Terra holds `[74,76,83,100]`. It plans to spend 83 while leaving lower cards that can use another row. An accepted memory-only update is followed by selection 83, then selection 76 on the next play. Resolution places 83 on row 0 and 76 on row 1, both without penalties. The alternative of playing 74 first uses the same current destination, but would not itself put 76 below that row's new tail; the 83-first plan deliberately preserves that possibility. This is evidence of planning beyond immediate risk, although the note's blanket safety language is too strong. See evidence E1.

**Adjacent cards: a positive example.** In G0 H4 P6, Sol selects 62 above tail 61 and explicitly notices that no integer can intervene in that gap. It correctly becomes the fourth card; Terra subsequently adds 72 as the fifth. The adjacent-card observation is sound, although removal of the destination row by an earlier below-all card remains a general caveat to absolute guarantees. See E2.

**Exploratory score-position check.** Terra's mean normalized selection rank is similar when strictly leading (0.373, 339 selections) and when behind the lowest-scoring opponent (0.367, 313 selections). Sol's is higher when leading (0.344, 144 selections) than when trailing (0.261, 533). These unadjusted groups differ in hands, play numbers, and table states; they do not establish a causal response to the score or justify describing a model as aggressive or conservative.

**Interpretation.** Spending high cards and accepting cheap resets plausibly contributes to Terra's fewer sixth-card penalties. The trace demonstrates examples of useful option preservation, but does not establish that one ordering policy is universally better. Sol's cheapest-row choices and early disposal of low cards are strengths worth preserving.

**Improvement and experiment.** Test a compact instruction to compare an immediate low-risk move with a move preserving another placement route. Measure later forced captures, hand penalties, and request cost. The tradeoff is additional analysis and the possibility of overvaluing speculative future flexibility.

### Finding 3: Clear mistakes occur in winning games, including a missed guaranteed win

Only 4/293 row choices exceed the displayed minimum: Luna 3/94, Terra 1/115, Sol 0/84. All 32 final-play row choices select a minimum-cost row. Nonminimum choices can be strategic, so these four are not automatically four mistakes.

Two Luna examples warrant scrutiny:

- **G1 H2 P7:** Luna chooses a nine-point row while explicitly listing alternatives costing five, four, and one. The response supplies no convincing reason for paying eight extra immediate points. Its hand still contains 8, 62, and 76, so a full-horizon optimality claim would require more than the immediate cost comparison. See E3.
- **G10 H4 P6:** Luna says row 3 minimizes penalties, then takes six points despite two displayed two-point rows. This is a clear cost-ranking error; the trace supplies no longer-term justification for the extra four immediate points. See E4.

Luna's remaining nonminimum choice, G2 H5 P8, pays seven rather than six and has no readable reasoning. It is not confidently classified as an error. The report does not present a complete model-by-model error rate from these targeted cases.

**Terra's missed guaranteed win.** In G5 H4 P9, Terra's accepted notebook plans to take one-point row 1, but its subsequent accepted row-choice action takes nine-point row 0. The pending cards—Terra 37, Luna 88, Sol 100—are already public. Terra has 73 left for the final play. There is no readable explanation for the departure from its plan. See E5.

Choosing row 1 would have produced this fully determined result for the current play:

| Player | Total after resolving the already-public P9 cards |
|---|---:|
| Luna | 44 |
| Terra | 27 |
| Sol | 72 |

After the replacement of row 1 by 37, Luna's 88 would extend row 2. Sol's 100 would capture the still-full row 0 for nine points. The match would therefore end after the remaining final play.

An exhaustive local calculation checked all **5,112 feasible ordered pairs of opponents' unknown final card values**, including every legal subsequent below-all row choice: 13,098 possible resolutions. Terra could finish with at most **37**, while Luna would remain at least 44 and Sol at least 72. Thus the one-point row choice guaranteed a sole victory at the end of that hand.

This conclusion uses information already supplied to Terra plus a complete enumeration of the remaining uncertainty. It is not a hindsight replay against opponents' actual final cards, and it does not invent an exact counterfactual final score.

The actual nine-point choice prevents Sol's P9 capture and prolongs the match. Terra eventually wins G5 by only one point. This is a demonstrably avoidable decision error hidden by a winning final outcome.

**Improvement and experiment.** Display minimum-cost rows explicitly and ask for a justification when choosing a more expensive one. Near the endpoint, evaluate already-revealed pending cards and guaranteed outcomes. Measure dominated choices and missed guaranteed wins. Do not force minimum-cost choices universally: changing row structure or opponents' penalties can justify a more expensive row.

### Finding 4: Static safety is not safety under simultaneous selection

Across all models, **10/1,172 selections** aimed at a row currently containing only one or two cards nevertheless captured a row. The per-model counts are Luna 4/424, Terra 3/390, and Sol 3/358. These outcomes require destination changes rather than simply enough cards filling the original short row.

**Reasonable choice, adverse simultaneous outcome.** In G0 H6 P5, Luna selects 96 above singleton 90. Terra's lower 18 takes that singleton for three points. Luna's destination changes to the full row ending 51, costing eight points. Luna's move is reasonable, but its recorded focus on cards above 90 misses the below-all reset route. Selection sequence 198 and capture resolution sequence 201 are different: the capture occurs while resolving the row-choice transaction, not when Luna selects. See E6.

**Incorrect certainty.** In G4 H2 P5, Terra records that 104 safely fills the four-card row ending 94 because no card can follow/capture it. Lower opponent cards resolve before 104: Sol's 99 adds the fifth card, and Terra takes ten points. The previous notebook also incorrectly excludes 84 because it was played in the previous hand, even though every hand uses a fresh shuffled deck. See E7.

**Risk acknowledged, gamble lost.** In G10 H5 P8, Sol's selection of 104 explicitly acknowledges the 15-point downside and hopes an opponent will clear the full row first. Neither does. This is risky but defensible reasoning, not evidence that Sol believes a high card permits choosing a row. The 65 alternative is more attractive for immediate penalties, but leaves difficult high cards for later. See E8.

**Correct outcome for the wrong reason.** In G0 H1 P10, Terra records that 78 targets row 2 ending 70, overlooking full row 0 ending 75. Luna's lower 37 happens to remove row 0, so 78 actually does reach row 2 safely. The reasoning was wrong for the observed table even though the final destination matched the forecast. Terra had only one card left, so the calculation error did not change its available card selection. See E9.

These cases separate errors in stated rules or calculations from reasonable moves with bad simultaneous outcomes. They do not support an exhaustive error census. In particular, no run-wide claim is made that a model systematically believes high cards allow arbitrary row choice.

#### Offline sensitivity checks

Three positions were evaluated with 10,000 sampled pairs of plausible remaining opponent hands per position, using only the analysed player's own hand and current-hand public information. No opponents' actual hidden hands were used.

The table reports immediate expected own penalties. In each cell the chosen card's estimate is first, followed by the alternative's estimate.

| Position and alternatives | Random opponent card | Smallest-card policy | Immediate-cost greedy policy |
|---|---:|---:|---:|
| Terra G4 H2 P5: **104 chosen** / 96 | 2.43 / 1.52 | 0.01 / 0.01 | 0.008 / 0.005 |
| Sol G10 H5 P8: **104 chosen** / 65 | 7.30 / 1.63 | 14.23 / 1.25 | 14.23 / 1.31 |
| Luna G0 H6 P5: **96 chosen** / 103 | 1.46 / 1.21 | 7.30 / 7.30 | 0.74 / 0.70 |

Method:

- Sample two remaining hands uniformly without replacement from 1–104, excluding the player's hand and current-hand public cards. Their sizes equal the opponents' remaining hand sizes.
- Compare random selection, smallest-card selection, and selection minimizing current-table capture cost, breaking ties toward the smallest card.
- Resolve all selected cards in ascending order. Below-all choices take a minimum-cost row, breaking ties toward the lowest row index.
- Use the same sampled hands across candidate own cards. Report immediate penalties only.

These policies give markedly different estimates. The sampling does not condition remaining hands on how likely the opponents' earlier choices would have been under each policy. It does not model future plays, strategic row-choice tie-breaking, or match win probability. It therefore does not establish a uniquely optimal move. Sol's 65 alternative looks better immediately under all three policies, but that does not prove it maximizes its chance of winning the match.

**Improvement and experiment.** Evaluate two hazards separately: earlier cards filling/resetting the intended row, and below-all cards removing its destination. Restrict public-card exclusions to the current hand. Measure false safety claims and captures from apparently short-row selections. A deterministic calculator can eliminate current-table arithmetic errors, but it must not present its static output as a guarantee about simultaneous resolution.

## 4. Memory, reasoning, and efficiency

### Finding 5: Memory supports some correction, but mostly adds duplication and calls

Luna and Sol make **zero accepted memory updates**. Terra makes **232**, in 11/16 games:

| Terra memory metric | Result |
|---|---:|
| Updates accompanying a game action | 168 |
| Memory-only calls | 64 |
| Subsequent requests with unchanged game observation | 64 |
| Replacement notebook length, mean / median | 277 / 281 characters |
| Largest replacement notebook | 496 characters |
| Supplied notebook length across all Terra requests, mean / median | 180 / 230 characters |
| Subsequent-hand starts receiving a nonempty notebook | 43/65 |
| Updates mentioning rows or tails | 213/232 |

The row/tail count is a lexical measure, not an estimate of the exact fraction of redundant characters. Many notes also reproduce hands, scores, and public plays already supplied by the observation. The notes stay far below the 4,000-character limit; notebook overflow is not an observed problem.

Concrete accepted chains show both value and limitations:

1. **Correction without new game information, G4 H3 P3.** Sequences 88–90 repeatedly rewrite the plan for 35. The first two notes wrongly use internal card 34 as row 1's endpoint; the third corrects the destination to row 2 ending 28. Sequence 91 finally selects 35. Its reasoning explicitly refers to the corrected notebook. The eventual placement changes again because Sol's lower 16 removes the destination row. This is evidence that the notebook was consulted, but the intended card remains 35 throughout; improved action quality is not demonstrated. See E10.
2. **Forecast presented as a completed event, G7 H3 P3.** Sequence 82 writes “Played 79” without selecting it. The next request still contains 79 in hand. Sequence 83 explicitly corrects that false claim, then sequence 84 selects 79. This is real local correction, but it repairs an error introduced by the memory-only workflow. See E11.
3. **A stale error survives a hand boundary, G0 H1 P10 to H2 P1.** The incorrect 78-to-row-2 explanation in E9 is accepted and supplied in the next hand. Terra then chooses 95 using the new table. The old note's presence is verified; influence on the new action is not demonstrated.
4. **Repeated calls without changing the plan, G5 H4 P8.** Sequences 143, 144, and 145 rewrite the notebook around card 64. Sequence 146 finally selects 64. No game observation changes between these calls. See E12.

The G4 H1 P8 note also says 93 would certainly capture a row containing four cards. In H1 P10, an accepted replacement correctly says 93 makes that unchanged row's fifth card. That is a factual correction, but it occurs on a forced single-card decision and is not explicitly framed as a learned general rule. Similar occupancy and destination errors occur in later hands. See E13.

**Interpretation.** There is evidence of local correction and consultation of supplied notes, but no clean evidence of durable learning within a match. A bad note preceding an action does not establish memory causality. Detailed causal attribution would require memory ablation or controlled replay. Notebook verbosity itself is not shown to reduce accuracy; the observed notes are short, and no systematic factual-error annotation was performed across all 232 updates.

**Improvement and experiment.** Require one game action per response, with an optional accompanying memory update. Keep notebook entries to useful plans, hypotheses, and information missing from the observation; label planned, committed, and resolved events separately. Test paired performance with memory disabled, optional, and restricted to action-accompanying updates. Measure repeated calls, factual errors, cost, and hand penalties. The tradeoff is losing some opportunities for useful reanalysis such as E10; an experiment should check that eliminating loops does not worsen decisions.

### Recorded reasoning and action coherence

Readable reasoning is supplied for 688/904 Luna requests, 521/989 Terra requests, and 573/894 Sol requests. The rest must not be interpreted as decisions made without reasoning. Responses also contain encrypted reasoning data, which was not decoded. Visible summaries are evidence of what the model wrote, not complete or necessarily faithful accounts of its internal process.

The evidence includes:

- correct local calculations, such as Sol's adjacent 61→62 placement;
- stated preferences matching subsequent selections, such as Terra's 83-first plan;
- explicit consultation of corrected notes in G4 H3 P3;
- action/plan divergence without explanation, notably Terra's G5 H4 P9 row choice;
- internally inconsistent or mistaken explanations even where an eventual move is reasonable.

**Exploratory length check.** Reasoning-token counts were compared with realized play penalties after removing within-stratum means for model, game, play number, and current destination-row length, treating below-all selections separately. Only strata with at least two decisions were retained, and forced single-card turns were excluded. The residual correlations were Luna −0.067 (461 selections), Terra +0.048 (475), Sol +0.007 (464).

These near-zero associations do not show that longer reasoning helps or harms. The controls do not capture hand difficulty, card gaps, row costs, future flexibility, or all forms of simultaneous risk. Textual error rates also cannot be compared naively when reasoning visibility differs substantially across models.

### Quality, cost, and latency

| Metric | Luna | Terra | Sol |
|---|---:|---:|---:|
| Requests | 904 | 989 | 894 |
| Accepted game actions, excluding memory | 904 | 925 | 894 |
| Input tokens | 1,725,959 | 1,957,404 | 1,697,715 |
| Output tokens, including reasoning | 291,403 | 333,173 | 224,946 |
| Reported reasoning tokens | 273,229 | 282,117 | 206,982 |
| Reported cached input tokens | 0 | 0 | 0 |
| Request latency, median / p95 | **5.57 / 11.46 s** | 6.07 / 11.61 s | 6.33 / 12.66 s |
| Maximum request latency | 21.19 s | 19.70 s | 38.97 s |
| Provider request time, summed | 92.43 min | 106.54 min | 104.68 min |
| Arena decision time, summed | 92.71 min | 106.84 min | 104.95 min |
| Sum of provider `usage.cost` | **0.7726** | 8.8052 | 6.4043 |
| Repairs / rejections / failures / timeouts | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |

Provider `usage.cost` is a recorded usage estimate, not a verified bill; the trace does not explicitly label its currency. The total is 15.9821 reported cost units. Terra's reported cost is **11.4 times Luna's**, for a performance advantage over Luna that remains uncertain in this sample. Sol is more expensive and slower per request than Luna without a stronger aggregate result in these matches.

Terra's memory-only calls consume 131,871 input tokens, 28,966 output tokens, **474 seconds** of provider time, and 0.6724 reported cost, or 7.6% of its total reported cost. Some provide reanalysis; none provide new game information.

Timing is counted once per response/decision, not once per tool call. The runner records decision duration only once within a batch; additional action records have null timing. Provider duration, arena decision duration, and wall-clock match time are distinct quantities.

Individual matches last 14.11–22.73 minutes, median 19.25. The first game starts at 17:31:32 UTC and the last ends at 18:54:20 UTC, an elapsed run span of 82.81 minutes. Four concurrent matches overlap, so summed per-model provider time is not elapsed run time.

There are no observed recovery events from which to compare repair quality or provider compatibility. The schemas worked for all three models in this run.

## 5. Evidence register

All player IDs are fixed across the run: Luna is `player_1`, Terra `player_2`, Sol `player_3`. `H` and `P` denote hand and play. Sequence numbers are `server_action_seq`; requests are identified below by full `request_id`. JSONL line numbers refer to the model trace unless explicitly identified as event lines.

### E1. Preserving a lower placement route

**Match:** `arena_7`; **player:** `player_2`. [Model trace](arena_20260906T173132Z_6769_7.model.jsonl), [accepted actions](arena_20260906T173132Z_6769_7.actions.jsonl), [events](arena_20260906T173132Z_6769_7.jsonl).

| Hand / play | Action sequence(s) | Request ID | Request / response line | Proposed calls |
|---|---|---|---|---|
| H1 / P7 | 26 | `7a4db3c2-d58b-4eba-b662-757abf6e3da8` | 67 / 68 | update_memory |
| H1 / P7 | 27 | `8a7129bb-cf86-431a-ae12-ecf059edca27` | 70 / 71 | select_card(83) |
| H1 / P8 | 30 | `37abebed-7ea3-4e29-843f-84fabc4808e5` | 79 / 80 | select_card(76) |

### E2. Adjacent-card anticipation

**Match:** `arena_0`; **player:** `player_3`. [Model trace](arena_20260906T173132Z_6769_0.model.jsonl), [accepted actions](arena_20260906T173132Z_6769_0.actions.jsonl), [events](arena_20260906T173132Z_6769_0.jsonl).

| Hand / play | Action sequence(s) | Request ID | Request / response line | Proposed calls |
|---|---|---|---|---|
| H4 / P6 | 130 | `5671abcc-4532-404d-853b-373760b866e5` | 361 / 362 | select_card(62) |

### E3. Luna chooses nine points over one

**Match:** `arena_1`; **player:** `player_1`. [Model trace](arena_20260906T173132Z_6769_1.model.jsonl), [accepted actions](arena_20260906T173132Z_6769_1.actions.jsonl), [events](arena_20260906T173132Z_6769_1.jsonl).

| Hand / play | Action sequence(s) | Request ID | Request / response line | Proposed calls |
|---|---|---|---|---|
| H2 / P7 | 63 | `0bc05ab7-fdaf-446d-b191-40f3bbf7f875` | 178 / 179 | choose_row(2) |

### E4. Luna incorrectly minimizes with six points

**Match:** `arena_10`; **player:** `player_1`. [Model trace](arena_20260906T173132Z_6769_10.model.jsonl), [accepted actions](arena_20260906T173132Z_6769_10.actions.jsonl), [events](arena_20260906T173132Z_6769_10.jsonl).

| Hand / play | Action sequence(s) | Request ID | Request / response line | Proposed calls |
|---|---|---|---|---|
| H4 / P6 | 142 | `8435aaf2-ca71-4b3d-8467-c92d97338262` | 382 / 383 | choose_row(3) |

### E5. Terra overlooks a guaranteed win

**Match:** `arena_5`; **player:** `player_2`. [Model trace](arena_20260906T173132Z_6769_5.model.jsonl), [accepted actions](arena_20260906T173132Z_6769_5.actions.jsonl), [events](arena_20260906T173132Z_6769_5.jsonl).

| Hand / play | Action sequence(s) | Request ID | Request / response line | Proposed calls |
|---|---|---|---|---|
| H4 / P9 | 149 | `3aa2a57c-23e8-4634-8f01-bd54e50372a0` | 403 / 404 | update_memory |
| H4 / P9 | 150 | `a0391c10-f921-4226-a038-90840ee00583` | 406 / 407 | select_card(37) |
| H4 / P9 | 152 | `4ba98a1a-cf96-4ef4-b387-922b8994d956` | 412 / 413 | choose_row(0) |

### E6. Short-row removal redirects Luna

**Match:** `arena_0`; **player:** `player_1`. [Model trace](arena_20260906T173132Z_6769_0.model.jsonl), [accepted actions](arena_20260906T173132Z_6769_0.actions.jsonl), [events](arena_20260906T173132Z_6769_0.jsonl).

| Hand / play | Action sequence(s) | Request ID | Request / response line | Proposed calls |
|---|---|---|---|---|
| H6 / P5 | 198 | `4c39d261-e213-43a1-853b-de4efc7d823f` | 544 / 545 | select_card(96) |

### E7. Terra incorrectly treats 104 as safe

**Match:** `arena_4`; **player:** `player_2`. [Model trace](arena_20260906T173132Z_6769_4.model.jsonl), [accepted actions](arena_20260906T173132Z_6769_4.actions.jsonl), [events](arena_20260906T173132Z_6769_4.jsonl).

| Hand / play | Action sequence(s) | Request ID | Request / response line | Proposed calls |
|---|---|---|---|---|
| H2 / P4 | 53 | `07e2a47b-55a5-4e78-826b-71d8ea47a405` | 142 / 143 | update_memory |
| H2 / P4 | 54 | `6702a3fd-046b-4c9b-a8d4-cfb76703020c` | 145 / 146 | select_card(86) |
| H2 / P5 | 57 | `83b3bb15-3955-4c54-8575-1888056738b1` | 154 / 155 | update_memory |
| H2 / P5 | 58 | `3f8596d3-4d04-400b-875c-dc948208810e` | 157 / 158 | select_card(104) |
| H2 / P6 | 61 | `61120e6b-a076-4abb-b360-1d0e83a065e7` | 166 / 167 | update_memory |
| H2 / P6 | 62 | `8771df6b-1814-4405-8474-cc7a719c1709` | 169 / 170 | select_card(40) |

### E8. Sol knowingly gambles on a reset

**Match:** `arena_10`; **player:** `player_3`. [Model trace](arena_20260906T173132Z_6769_10.model.jsonl), [accepted actions](arena_20260906T173132Z_6769_10.actions.jsonl), [events](arena_20260906T173132Z_6769_10.jsonl).

| Hand / play | Action sequence(s) | Request ID | Request / response line | Proposed calls |
|---|---|---|---|---|
| H5 / P8 | 185 | `d49def0d-cb64-4c4b-8b10-5afd81b475f3` | 502 / 503 | select_card(104) |

### E9. Wrong calculation succeeds; note crosses hand boundary

**Match:** `arena_0`; **player:** `player_2`. [Model trace](arena_20260906T173132Z_6769_0.model.jsonl), [accepted actions](arena_20260906T173132Z_6769_0.actions.jsonl), [events](arena_20260906T173132Z_6769_0.jsonl).

| Hand / play | Action sequence(s) | Request ID | Request / response line | Proposed calls |
|---|---|---|---|---|
| H1 / P10 | 36, 37 | `9a5bee01-6954-44ff-a391-047413877bf5` | 94 / 95 | update_memory; select_card(78) |
| H2 / P1 | 41 | `68b04499-abad-4f07-b3e4-ad01edfd6fa8` | 106 / 107 | select_card(95) |

### E10. Repeated notes correct a destination

**Match:** `arena_4`; **player:** `player_2`. [Model trace](arena_20260906T173132Z_6769_4.model.jsonl), [accepted actions](arena_20260906T173132Z_6769_4.actions.jsonl), [events](arena_20260906T173132Z_6769_4.jsonl).

| Hand / play | Action sequence(s) | Request ID | Request / response line | Proposed calls |
|---|---|---|---|---|
| H3 / P3 | 88 | `2b3cf1b0-f8df-4bac-beb6-679727c13c3a` | 238 / 239 | update_memory |
| H3 / P3 | 89 | `a6055012-0d89-4ba6-a739-faa8b845f19b` | 241 / 242 | update_memory |
| H3 / P3 | 90 | `d8009e9d-bf97-4e68-965e-34c2b5882406` | 244 / 245 | update_memory |
| H3 / P3 | 91, 92 | `4de6c340-6978-491c-8cb0-6cf6559d9772` | 247 / 248 | select_card(35); update_memory |

### E11. An unperformed action is corrected

**Match:** `arena_7`; **player:** `player_2`. [Model trace](arena_20260906T173132Z_6769_7.model.jsonl), [accepted actions](arena_20260906T173132Z_6769_7.actions.jsonl), [events](arena_20260906T173132Z_6769_7.jsonl).

| Hand / play | Action sequence(s) | Request ID | Request / response line | Proposed calls |
|---|---|---|---|---|
| H3 / P3 | 82 | `8e4f3516-9a3a-495f-9132-49d89905c1c4` | 232 / 233 | update_memory |
| H3 / P3 | 83 | `fb6bd355-8aac-4124-8b4e-19ad974e2a02` | 235 / 236 | update_memory |
| H3 / P3 | 84 | `eee4f51e-ccb7-426c-865b-20f58d32276f` | 238 / 239 | select_card(79) |

### E12. Three notebook-only calls precede the same planned move

**Match:** `arena_5`; **player:** `player_2`. [Model trace](arena_20260906T173132Z_6769_5.model.jsonl), [accepted actions](arena_20260906T173132Z_6769_5.actions.jsonl), [events](arena_20260906T173132Z_6769_5.jsonl).

| Hand / play | Action sequence(s) | Request ID | Request / response line | Proposed calls |
|---|---|---|---|---|
| H4 / P8 | 143 | `491314aa-c22b-4a2e-9f65-7067731df632` | 385 / 386 | update_memory |
| H4 / P8 | 144 | `25540eff-9127-4351-ab2c-8915576d08a1` | 388 / 389 | update_memory |
| H4 / P8 | 145 | `d03e0e50-0436-4e28-b8c0-8ccde415b7ca` | 391 / 392 | update_memory |
| H4 / P8 | 146 | `7fd8310f-599e-4e13-a08b-751d80eaeba6` | 394 / 395 | select_card(64) |

### E13. Fifth-versus-sixth correction

**Match:** `arena_4`; **player:** `player_2`. [Model trace](arena_20260906T173132Z_6769_4.model.jsonl), [accepted actions](arena_20260906T173132Z_6769_4.actions.jsonl), [events](arena_20260906T173132Z_6769_4.jsonl).

| Hand / play | Action sequence(s) | Request ID | Request / response line | Proposed calls |
|---|---|---|---|---|
| H1 / P8 | 29 | `df24a1eb-a0cb-4972-963d-88d0ef77ac19` | 79 / 80 | update_memory |
| H1 / P8 | 30 | `108800b9-00e9-411c-bae0-a5a96020d2d9` | 82 / 83 | select_card(47) |
| H1 / P10 | 37, 38 | `ba62cbda-8f7b-4661-aeb8-5df69eb80338` | 103 / 104 | select_card(93); update_memory |

## 6. Calculation details and limits

### Statistical definitions

- Final scores are the `totals` in each match's final `match_ended` event.
- Hand scores come from `hand_ended`; capture counts, reasons, and sizes come from `row_taken`. Summed capture penalties agree with every final score.
- A card play is one accepted `select_card` action. Row choices and notebook writes are not additional card plays.
- Pooled penalties per hand divide total model penalties by 81; penalties per play divide by 810. Game-weighted hand rates instead average each game's final score divided by its own hand count.
- A costly capture is defined here as at least ten points; the at-least-fifteen threshold is also reported. These descriptive thresholds are not fitted outcome models.
- Non-forced selection metrics exclude the 81 single-card turns per model. Current destination, gap, and row occupancy are calculated from the player's supplied table before simultaneous resolution.
- A strict lead means the player's banked-plus-current-hand score is below both opponents'. Trailing means above the minimum opponent score; tied-for-lowest positions form a separate group.
- Memory lengths count Unicode characters. Supplied notebook lengths include empty notebooks; update lengths measure the complete replacement string.
- Reported output tokens include reasoning tokens; reasoning tokens are not added again when calculating usage. Reported cache counts are zero, which is not a claim about undisclosed provider internals.
- Standard deviations use the sample definition. Percentiles use linear interpolation between ordered observations.

### Bootstrap specification

The local bootstrap uses Python's random generator seeded with 6769. It draws 20,000 samples of 16 indices with replacement for each paired comparison. In the calculation order Luna–Terra, Luna–Sol, Terra–Sol, it computes the mean final-score difference for each sample, then takes the 2.5th and 97.5th interpolated percentiles. Signs are reversed where necessary to express the report's “advantage” convention. The random generator continues between comparisons.

The leave-one-game-out check removes each of the 16 games in turn and recalculates the mean difference over the remaining 15. It tests sensitivity to a single game; it is not independent validation on new games.

### Offline simulation and endgame specification

The three simulation positions are evaluated in the order Terra G4 H2 P5, Sol G10 H5 P8, and Luna G0 H6 P5, with a Python random generator seeded with 6769 and continued across positions. Each position uses 10,000 sampled pairs of disjoint remaining hands. Policies and limitations are described in finding 4. Tiny differences such as 0.008 versus 0.005 should not be interpreted as meaningful superiority; estimates are rounded and policy-dependent.

The endgame calculation in finding 3 is an exhaustive enumeration, not a Monte Carlo estimate. At the G5 H4 P9 row choice, the public information leaves 72 feasible values for the opponents' two final cards, yielding `72 × 71 = 5,112` ordered pairs. The alternative current-play resolution is deterministic. Subsequent below-all choices branch over all four legal rows, yielding 13,098 final resolutions. The guaranteed-winner claim holds throughout this superset of possible continuations; it does not rely on an assumed opponent policy.

### What remains unanswered

- General model superiority, independent of seats, dealt cards, opponent lineup, or provider defaults.
- Whether Terra's notebook causes its advantage, harms particular decisions, or merely accompanies a different strategy.
- An exhaustive rate of basic reasoning mistakes. Aggregate action and outcome statistics cover the full run; qualitative error findings are targeted case studies.
- Whether models reliably learn opponent selection policies. There are tentative comments about low cards, but no controlled evidence that such hypotheses improve later choices.
- Whether high-card spending or cheap captures improve full-hand outcomes under different opponent policies.
- Whether longer reasoning improves decisions after adequate adjustment for difficulty. The exploratory controls used here are incomplete.
- Whether a smaller observation history or structured notebook reduces cost without losing useful public information.
- Communication ability and recovery behaviour, because neither communication nor failures occurred.

Errors in examples with accurate displayed tails, counts, or costs are model interpretation or decision problems. Memory-only loops are permitted by the prompt, schema, and scheduling design; they are not rejected transactions or provider failures. Repeated old facts reflect the replacement-notebook design and optional updates. No evidence justifies blaming missing observations or inventing a harness failure to explain these outcomes.

The most promising exploratory findings are the heavier catastrophic-hand tail for Sol, the preservation of alternative placement routes through high-card spending, and correct outcomes reached through incorrect calculations. These should guide controlled experiments rather than be treated as established causal mechanisms.

## 7. Prioritised improvements and experiments

### Cheap prompt and observation changes

1. **Make basic calculations explicit.** Display each row's tail separately, identify minimum-cost rows, distinguish “becomes fifth” from “captures five,” and label destination calculations as conditional on the current table. Measure incorrect destination/cost claims and dominated row choices. Extra presentation should be kept compact to avoid unnecessary input growth.
2. **Clarify simultaneous and cross-hand reasoning.** Require consideration of lower cards filling a row and below-all cards removing its destination. State that cards from previous hands are available again. Measure false safety claims and captures from apparently short rows.
3. **Label notebook statements by status.** Separate plans from committed selections and resolved outcomes; avoid copying authoritative rows and scores unless needed for a specific comparison. Measure stale or premature claims and notebook token use.

### Larger workflow and architecture changes

4. **Require game-action progress per response.** Permit one optional notebook update alongside the legal game action. Evaluate whether removing the 64 observed extra calls saves cost without losing useful correction or worsening penalties.
5. **Add deterministic calculation support.** Compute current-table destinations and penalties outside the model; near the endpoint, evaluate public pending cards and guaranteed outcomes. Measure missed guaranteed wins and arithmetic/rule errors. Keep hidden-card uncertainty explicit rather than turning a static calculation into a safety guarantee.

### Controlled follow-up experiments

6. **Rotate seats on matched deals.** Use all six model-seat permutations and enough distinct seeds to estimate paired game effects. Report both fixed-hand penalties and target-score wins.
7. **Ablate memory and history separately.** Compare no notebook, optional notebook, and action-accompanying notebook; separately test compact versus full public history. Hold prompts, provider settings, and deal/seat assignments fixed. Measure quality, calls, tokens, latency, and notebook accuracy.
8. **Test decision support on a preregistered position set.** Include crowded rows, below-all resets, adjacent cards, cheap captures that buy future options, and near-terminal row choices. Evaluate under several opponent policies and then validate with full matches. Do not select only positions on which a proposed strategy already looks favourable.
