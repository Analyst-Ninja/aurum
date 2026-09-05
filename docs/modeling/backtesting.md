# Backtesting — turning a ranking into a strategy, and then distrusting it

> **Design — not yet built.** Specifies `src/modeling/backtest/`, implemented under
> [#56](https://github.com/Analyst-Ninja/aurum/issues/56). Entry point:
> [`modeling-design.md`](modeling-design.md).
>
> **Scope reminder:** AURUM emits decisions; it never executes trades. This module simulates.

Contents:

1. [Prediction quality is not economic value](#1-prediction-quality-is-not-economic-value)
2. [Input: out-of-sample predictions only](#2-input-out-of-sample-predictions-only)
3. [Portfolio construction](#3-portfolio-construction)
4. [Costs, and the number that matters](#4-costs-and-the-number-that-matters)
5. [What the report contains](#5-what-the-report-contains)
6. [Factor attribution](#6-factor-attribution)
7. [Three reality checks](#7-three-reality-checks)
8. [Why no backtesting framework](#8-why-no-backtesting-framework)
9. [Known gaps](#9-known-gaps)

---

## 1. Prediction quality is not economic value

[`modeling-design.md`](modeling-design.md) §5 measures whether the model **ranks stocks correctly**.
This document measures whether a portfolio built on that ranking **makes money after frictions**.

Conflating the two is the most common way a research pipeline lies to the person who built it.
The two can diverge badly, and each of these is an ordinary outcome, not a pathology:

- A high ICIR whose signal lives entirely in the smallest, least liquid names — real, and
  untradeable at any size.
- A high ICIR that requires 200% daily turnover — real, and consumed entirely by costs.
- A high ICIR concentrated in two crisis years — real, and a strategy that does nothing for a
  decade.
- A good decile spread that is 95% a bet on one sector — real, and not a stock-selection signal.

None of these is visible in an IC number. All of them are visible here.

> **A model with an ICIR of 0.9 whose strategy loses money after costs is an expected outcome, not
> a bug.** The purpose of this module is to find that out before anyone believes otherwise.

---

## 2. Input: out-of-sample predictions only

Concatenate the walk-forward out-of-sample predictions across evaluation folds into one continuous
per-(symbol, date) series. Each prediction comes from a model that had never seen that date, with
purge and embargo applied — see [`training-and-retraining.md`](training-and-retraining.md) §2.

**Nothing in-sample ever enters the backtest.** An in-sample equity curve goes up. It always goes
up. It is worth nothing.

The 24-month final holdout is backtested and **reported separately** from folds 121–297. The
evaluation-fold backtest informs design; the holdout backtest is the number that gets quoted.

---

## 3. Portfolio construction

Baseline; each element is a config knob.

**The portfolio.** Rank predictions within each date. Long the top decile, short the bottom decile,
**dollar-neutral** (equal capital long and short, so the market's own move nets out) and
equal-weight. Alternatives supported: signal-weighted (position size proportional to predicted
rank), and sector-neutralized (rank within sector, which strips an implicit sector bet — see §6).

**Five overlapping tranches.** The signal has a five-day horizon, so rebalancing the whole book
daily would trade five times more than the signal justifies, and holding for five days with a
single rebalance would leave the strategy idle four days in five.

Instead: trade **one fifth of the book each day**, and hold each tranche for five days. The
portfolio always holds five vintages at different ages. Daily turnover is ~2/5 of the book — one
fifth exiting and one fifth entering — rather than 2×.

```
day 1: open tranche A ┐
day 2: open tranche B │  five vintages live at all times
day 3: open tranche C │  each held 5 days
day 4: open tranche D │  ~1/5 of book turns over daily
day 5: open tranche E ┘
day 6: close A, open F
```

This construction is shared with the long-short Sharpe in
[`modeling-design.md`](modeling-design.md) §5.1 — implemented **once**, in `evaluate/metrics.py`,
and imported here. Two implementations would drift apart on the turnover convention and quietly
report two different Sharpes.

**Constraints:**

| Constraint | Default | Why |
|---|---|---|
| Max weight per name | 2% | Prevents a single conviction from becoming the strategy |
| Sector weight vs universe | ±10% | Keeps it a stock-selection signal, not a sector bet |
| Position ≤ % of `adv_21d` | 5% | **The one that makes capacity meaningful.** Without it, "the strategy returns 12%" is a statement about a notional book that could never be filled |

---

## 4. Costs, and the number that matters

Per-side cost:

```
cost_bps = half_spread_bps + k · vol_21d · sqrt(participation)
```

The first term is the bid-ask spread you pay to cross. The second is **market impact** — the price
moves against you as you buy, more so for a volatile name and for a larger share of its daily
volume. The square-root form is the standard empirical shape.

**Sweep 0 / 5 / 10 / 20 bps per side and report the break-even cost.**

> "The strategy is profitable up to 14 bps per side" is a more useful sentence than any single
> Sharpe number, because it tells you how much execution quality the strategy requires — and
> whether it survives being traded by someone who is not a specialist.

A strategy whose break-even is 2 bps does not exist. One whose break-even is 50 bps is either
excellent or has a bug, and §7 is how you find out which.

Assumptions, stated as assumptions rather than facts: **borrow cost is zero** and **every S&P 500
name is shortable**. Both are approximately true for large caps and neither is exactly true.

---

## 5. What the report contains

| Metric | Note |
|---|---|
| Annualized return, volatility | |
| **Sharpe** | Net of cost, at each swept level |
| Sortino | Sharpe counting only downside volatility |
| **Max drawdown, and its duration** | Duration is the one people skip and the one that ends strategies — a 15% drawdown lasting three years is worse than a 25% one lasting two months |
| Calmar | Return / max drawdown |
| Annualized turnover | Sanity check on §3's tranche construction |
| Per-trade hit rate, average holding period | |
| **Capacity estimate** | The AUM at which modelled impact consumes gross alpha, given the 5%-of-ADV cap |
| **Yearly returns table** | See below |

**The yearly returns table is the single most honest artifact in the report.** One row per calendar
year, net return, Sharpe, max drawdown. It shows immediately whether a good aggregate number is one
strong decade and fifteen flat years — which no aggregate statistic will ever volunteer.

**The three upward biases from [`modeling-design.md`](modeling-design.md) §7 are restated at the
top of the report**, before any performance number: survivorship, the point-in-time lag
approximation ([#47](https://github.com/Analyst-Ninja/aurum/issues/47)), and overlapping labels.
Survivorship in particular is not a footnote here — `company_meta.csv` is today's S&P 500 applied
back to 2000, so every company that entered the index and then failed is missing from the short
book that would have profited from it.

Artifacts land in `models/{version}/backtest/`: `summary.json`, `yearly.csv`, `equity_curve.csv`,
`positions.parquet`, `tearsheet.png`.

---

## 6. Factor attribution

Regress the strategy's daily returns on:

- the market (`market_ret_1d`), and
- the decile-spread returns of the classic single factors — `mom_12_1`, `reversal_5d`,
  `market_cap` (size), `earnings_yield` (value).

**What the intercept means.** If it is significantly positive after these loadings, the model found
something the classic factors do not already capture. If it vanishes, the model has **repackaged a
known factor** at far higher complexity — which is a legitimate finding, and one the report must
state plainly rather than bury under a good-looking equity curve. A momentum strategy you can
implement with one `order by` is strictly better than the same strategy wrapped in 200 features and
a gradient booster.

A large positive loading on `market_ret_1d` in a supposedly dollar-neutral portfolio means the
neutrality is not working — usually a beta mismatch between the long and short books. That is a
bug, not a result.

---

## 7. Three reality checks

All three run before any result is quoted.

### 7.1 Randomization test

Shuffle the predictions **within each date** — preserving the number of positions, the dates, the
universe, and every cost — and rerun the backtest ~500 times. Report the actual Sharpe's percentile
against that null distribution.

This is the p-value that an equity curve does not show you. A strategy at the 60th percentile of
its own randomization null is noise with a nice-looking chart.

Shuffling *within* date is what makes this a test of the signal rather than of the construction: it
destroys the stock-picking information while leaving everything else identical.

### 7.2 Signal-lag test

Delay the signal by one day and rerun.

Real predictive signal at a five-day horizon decays gracefully — a one-day delay costs some
performance, not all of it. A strategy that **collapses** under a one-day lag is exploiting
same-bar information, and there is a leak upstream that the purge, the embargo and every dbt
lookahead test failed to catch.

### 7.3 Deflated Sharpe

Adjust the reported Sharpe for `n_configs_tried`, recorded in `metadata.json`.

Selecting the best of 200 configurations manufactures Sharpe out of noise: the *maximum* of 200
draws from a zero-mean distribution is comfortably positive. The deflated Sharpe asks what is left
after accounting for how many times you looked.

This is why `n_configs_tried` is recorded at training time rather than estimated later, and why
[`training-and-retraining.md`](training-and-retraining.md) §4 defers Optuna — a large automated
search inflates this number faster than it improves the model.

---

## 8. Why no backtesting framework

Vectorized pandas on a daily grid. **No zipline, vectorbt or backtrader.**

The whole problem is ~6,600 dates × ~450 names of daily data with a single rebalance rule. That is
a few array operations. An event-driven framework exists to handle intraday order books, partial
fills, multiple asset classes and live-trading parity — none of which apply, all of which cost a
heavy dependency, a data-adapter layer and a second set of semantics to learn.

The one thing to keep in mind while implementing: §7.1 runs the engine ~500 times, so
`engine.run()` should be allocation-light enough that the randomization test takes minutes.

---

## 9. Known gaps

| # | Gap | Cost | Fix |
|---|---|---|---|
| 1 | **Survivorship bias** — the universe is today's S&P 500 applied back to 2000 | Upward bias on every number here, largest single caveat. The short book is missing precisely the companies that failed | Ingest historical index membership |
| 2 | Fills assumed at `adj_close`, no slippage beyond the impact term | Overstates achievable execution | Intraday execution model, Phase 7 |
| 3 | Borrow cost zero, shortability universal | Net Sharpe is an upper bound | Real borrow data |
| 4 | Impact coefficient `k` is assumed, not calibrated | The capacity estimate is order-of-magnitude only | Calibrate against realized fills if the strategy is ever traded |
| 5 | No index-inclusion or corporate-action event handling beyond `adj_close` adjustment | Rare-event P&L is approximate | — |
| 6 | Equal-weight decile portfolios only, in the baseline | Simple and robust; may leave return on the table | Signal-weighted and risk-parity variants are config knobs, not defaults |

---

## See also

| Doc | Content |
|---|---|
| [`modeling-design.md`](modeling-design.md) | §5 predictive metrics, §7 the limitations restated here |
| [`training-and-retraining.md`](training-and-retraining.md) | §2 the purged folds that make these predictions out-of-sample; §8 the promotion gate this feeds |
| [`preprocessing-contract.md`](preprocessing-contract.md) | What the model saw |
| [`../warehouse/rationale/gold-models-rationale.md`](../warehouse/rationale/gold-models-rationale.md) | §5 targets and the leakage contract |
