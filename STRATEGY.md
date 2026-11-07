# Bot strategy: the target design (v2)

This is the settled plan. Build toward it; don't re-litigate it. Change it only when a
measured result contradicts it, and record why in the "Changes" section at the bottom.

## The problem, in the terms that matter
- Opponents are unknown bots, mostly LLM-written "equity" bots, some broken, a few strong.
- Each half is 1,000 hands and memory resets, so every opponent is learned from scratch.
- A match point is binary: a win needs a statistically significant edge; a loss costs a full
  point, a chop half. **Never losing matters more than maximal chips.**
- About 2-4 ms per decision on one slow CPU. Anything heavy is precomputed offline.

## Principle: estimate almost everything, tune almost nothing
Overfitting comes from tuned numbers bending toward the test opponents. The number of
tuned knobs the evidence can support is set by the number of *independent opponent families*
(about 5), not the number of hands. So v2 has **2 tuned knobs**. Everything else is either a
fact about the game or a fact about the current opponent.

| Kind | What | Where it comes from | Can it overfit? |
|---|---|---|---|
| Game facts | Preflop equity table (vs random, top 50%, 25%, 10% ranges) | Databricks, offline, exhaustive | No: it's the deck |
| Population priors | Average opponent's fold-to-bet, bet, raise and bluff rates, per street | Databricks, from the training families only | Mildly; live data overrides them |
| Live estimates | This opponent's same rates, plus bluff fraction from showdowns | `memory`, updated every hand | No: relearned every half |
| **Tuned knobs (2)** | `prior_strength` (pseudo-observations behind each prior), `mix_eps` (randomization when actions are near-equal) | `tune.py` | Yes: hence only two |

Fixed constants (not tuned, documented): equity realization 1.0 in position, 0.85 out of
position. Freeze any number whose ±20% change doesn't move points beyond noise.

## The decision layer
1. **Opponent range.** An opponent who bets x% of the time when checked to is modeled as
   betting their top x% of hands, plus a bluff fraction b. Hand strength = percentile from the
   preflop table. Each aggressive action narrows the range. One cheap rule that fits every
   style; no per-bot functions.
2. **Range-weighted equity.** The current Monte Carlo loop, but each sampled opponent hand is
   weighted by how consistent it is with that range (one table lookup per sample).
3. **Expected value, then choose.** For each legal action:
   - fold = 0
   - call = realization x equity x (pot + call) - call
   - raise (half or pot, from `obs.raise_to`) = P(fold) x pot + (1 - P(fold)) x
     (equity against their calling range x final pot - our cost)

   P(fold) and the range come from the opponent's posterior: the prior with
   `prior_strength` pseudo-observations, plus everything seen this half. Exploitation grows
   with evidence automatically; there is no separate exploit knob.
4. **Pick the best EV**, except within a small margin choose randomly with `mix_eps`, so that
   adaptive opponents can't read us.
5. **Safety, unchanged from v1:** never raise an exception; fall back to CHECK_CALL; pace the
   Monte Carlo against the time bank.

## Opponents and evaluation (fixed protocol)
| Family | What | Role |
|---|---|---|
| A. Generated | One parameterized bot (looseness, aggression, bluff rate, call-down, size preference), 50 random settings | Tuning + priors |
| B. Basic and broken | random, half, pot, station, slow (times out) | Tuning + priors |
| C. LLM-written | 3-5 bots written by other AI models from the starter code | **Exam: never tuned on, never used for priors** |
| D. League | Frozen past versions of mybot | Regression gate |
| E. Exploiter | One bot that tracks our tendencies and punishes them | Exploitability gate |

- Compare versions on **the same deals** (paired), per deal.
- Score = expected tournament points, weighted toward the expected field
  (C-like 50%, B 20%, A 20%, strong 10%).
- **Ship a change only if** it improves weighted points AND its worst family (especially
  C, D and E) is no worse than the current bot's.

## Build order
1. Submit v1 (the current bot) as insurance.
2. Families A, C, E plus per-deal paired comparison in the harness.
3. Databricks: preflop table, then population priors (from A + B only).
4. `mybot_v2.py` beside v1. Promote it into `agent.py` only by the rule above.
5. Tune the 2 knobs last, with the full protocol.

## How to train (the loop)
Everything below goes through the shared run queue, so runs never overlap.
1. **Measure** both bots on the full field: dashboard "Re-measure both bots", or
   `python tune.py run --bot mybot_v2 --seeds 2`.
2. **Find the leak**: `python databricks/export.py --hero mybot_v2 --seeds 2` (hindsight
   equity plus v2's own estimates, so calibration is visible), then the notebook or a query.
3. **Fix structure** in `mybot_v2.py` (Claude), and re-check calibration and the scoreboard.
4. **Tune the 2 knobs** only when structure is stable:
   `python tune.py sweep --bot mybot_v2 --trials 20 --seeds 2 --deals 1000`.
   It tunes on families A + B, then checks finalists on C + D + E with fresh deals; it
   recommends a change only if it beats the current settings by more than 2 standard errors on
   training, gains on held-out, and no held-out family gets significantly worse.
5. **Promote** v2 into `agent.py` only when its weighted score beats v1's and its worst family
   is no worse (dashboard scoreboard).

Measurement guarantees: v2 is deterministic (fixed samples per decision; it scales down only
if it falls behind the time budget) and uses common random numbers (the same spot always gets
the same simulations), so two settings are compared on identical deals and simulations; noise
is computed deal by deal.

## Changes
(date, what changed, the measurement that justified it)

- **2026-11-07: bluff share is predicted from aggression, not measured at showdowns.**
  Showdowns are censored by our own folds: `equity_bot` bluffs 13% of its raises overall but
  47% of those that reached showdown, identical to `random`. Across 7 opponents, bluff share =
  0.067 + 0.362 x postflop aggression fits with R^2 = 0.91, and aggression is observed on every
  action. (32,000-hand export, `data/databricks/20261107T220916Z_*`.)
- **2026-11-07: first v2 field result: 5.25 / 8 vs v1's 7.00 / 8** (4 seeds x 1000 deals,
  same deals). v1 stays in `agent.py`. Weakest: `tight_bot` (-1.5), `half`/`pot` (+1.3 vs +5.9).
  Diagnose with the dashboard's model view (`tools/trace_v2.py`).
- **2026-11-07: four structural fixes to v2, from a same-deals comparison with v1 and a
  calibration check of v2's own estimates against true equity.** (1) A re-raise was assumed to
  get folds like a first bet: predicted 33% vs actual 1% against tight_bot; now a separate
  fold-to-re-raise rate (priors 7/13/11/18% by street). (2) The range model ignored weakness:
  a check now caps the range, a call keeps the middle; checked-to equity error went from
  -9 points to about -3. (3) Median 26 simulations per decision, now 64 plus reuse within a
  betting round (each board scored once, tested against 4 hands). (4) Removed the hand-picked
  85% equity-realization constant. Field: 5.25 -> 6.50 / 8 (v1: 7.00).
- **2026-11-07: v2 made deterministic, with common random numbers.** Same settings now play
  identical hands; paired noise is about 1/3 lower per opponent (0.75 vs 1.11 bb/deal at 500
  deals). Opponent families A-E added (families.py): 12 generated styles incl. stack-off
  players, 3 all-in bots, 2 exam bots, an exploiter, v1 as league.
- **Known gap for training:** v2 still overestimates its equity when tight or equity-style
  bots bet (33% modeled vs 14% true against tight_bot): they bet on hand potential, which
  "current made hand" ranking undercounts. Candidate fix: learn a per-situation correction
  from exported data (a population estimate, not a tuned knob).
