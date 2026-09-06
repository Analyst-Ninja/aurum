# Modelling pipeline — the whole loop, in order

What to run, in what order, and what each step is actually doing. Every command is
copy-pasteable from the repo root. The numbers quoted as examples are from the run of
**2026-09-06** (`models/20260906-a7cffa3`), so you can check your output against a
known-good one.

The one-line version of the loop:

```
warehouse (dbt) ─▶ train ─▶ evaluate ─▶ select features (SHAP) ─▶ retrain narrowed
                                                                        │
                              promote ◀─ backtest ◀─ compare ◀─ evaluate┘
```

Two ideas explain why it has this shape:

- **Predicting well and making money are different questions.** Steps 3 and 9 answer
  them separately, in that order. A model can rank stocks correctly and still lose
  money once trading costs are paid.
- **Nothing is trusted until it is tested on data the model never saw.** The last
  24 months are held back from every fit. Every headline number comes from those.

---

## 0. Prerequisites

```bash
uv sync --group modeling            # ML deps (lightgbm, matplotlib, sklearn, scipy)
uv sync --group dbt                 # warehouse deps, separate on purpose
```

`.env` at the repo root supplies `HOST`, `PORT`, `AURUM_USERNAME`, `AURUM_PASSWORD`,
`SEC_USER_AGENT`. Postgres must be reachable — the whole pipeline reads from the
`aurum` database, not from files.

**What this is about:** the modelling code imports a ~400 MB ML stack that the
ingestion runtime must never pay for, so it lives in its own dependency group. dbt is
a CLI that `src/` never imports, so it lives in a third.

---

## 1. Build the warehouse

```bash
cd src/transformation/aurum_dwh
uv run --group dbt dbt seed          # company_meta, concept_map, selected_features
uv run --group dbt dbt build         # all models + ~237 tests
cd -
```

**What this is about:** turning raw Yahoo prices and SEC EDGAR filings into a clean
feature panel. Three layers — bronze mirrors the landing tables, silver engineers
technicals and financials, gold assembles the ML-ready marts.

**What you end up with:** `gold.mart_training_set` — ~2.9M rows, one per (symbol,
date), 193 features plus the prediction targets. This is the only thing the model
ever reads.

**How to know it worked:** `Done. PASS=... ERROR=0`. Two tests warn on documented
real-data outliers; that is expected.

---

## 2. Train the model

```bash
uv run python -m src.modeling.cli train -c src/modeling/configs/lgbm_xs_excess_5d.yaml
```

**What this is about:** learning to rank stocks. The target is `fwd_ret_5d_excess` —
each stock's next-5-day return *minus the market's*. Predicting the raw return would
just teach the model to predict the market, which is a different (and harder, and less
useful) job.

**How the data is split:** 321 monthly folds. Folds 1–120 are burn-in, 121–297 are the
evaluation window used to pick hyperparameters, and 298+ (the last 24 months) are the
**holdout** — locked away and never fitted on. Within the evaluation window, training
is walk-forward: the model only ever sees the past, with a purge and embargo gap so a
5-day label cannot straddle the boundary into its own validation window.

**Outputs** in `models/{version}/`: `model.txt` (the booster), `metadata.json`
(provenance, per-fold scores), `fold_predictions.parquet` (out-of-sample predictions
from each fold, kept so evaluation never has to refit).

**Runtime:** ~5 min on 40 features, ~30 min on the full 193.

---

## 3. Evaluate it

```bash
uv run python -m src.modeling.cli evaluate -c <config> --version <version>
```

**What this is about:** *does it rank correctly?* Purely statistical — no portfolio, no
costs yet.

**Metrics that matter here:**

| Metric | Reads as |
|---|---|
| **IC** (information coefficient) | Rank correlation between prediction and outcome, per day. 0.01–0.03 is a real equity signal; 0 is noise. |
| **ICIR** | IC divided by its own volatility, annualized. Consistency, not size. Above ~0.5 is interesting. |
| **Decile spread** | Return of the top 10% minus the bottom 10%. The signal in return units. |

Every number is reported against four **baselines** — a zero prediction, a momentum
sort, a reversal sort and a Ridge regression. A 193-feature model that merely ties a
momentum sort has told you momentum works, not that the model does.

**Output:** `models/{version}/metrics.json`. The holdout block is the headline; the
fold block is labelled `model_selection` because those folds chose the
hyperparameters and are therefore not honest out-of-sample.

---

## 4. Rank the features (SHAP)

```bash
uv run python -m src.modeling.cli select-features -c <config> --version <version>
```

**What this is about:** finding which of the 193 features the model actually uses, and
throwing the rest away. Fewer features means less noise to overfit to and a model you
can reason about.

**How it works, in three moves:**

1. **Sample and attribute.** Tree SHAP on ~200k rows, drawn as an equal quota from
   every date (the universe grows from ~320 names in 2000 to ~500 in 2026, so a
   uniform sample would silently rank on recent years). Each feature gets a
   `mean_abs_shap` — how much it moves predictions, on average.
2. **Prune collinear duplicates.** The warehouse ships most features in three variants
   (`ret_21d`, `ret_21d_z`, `ret_21d_decile`). SHAP splits credit across them, so each
   looks mediocre while jointly mattering. Features correlated above |ρ| > 0.95 are
   clustered and only the strongest member of a cluster can be selected.
3. **Cut.** Keep cluster leaders up to 95% of total importance, capped at 40. The
   artifact records which rule bound — on this run it was the **cap**.

**Outputs:** `models/{version}/shap/ranking.csv` (every feature, ranked, with its
cross-era stability and cluster id), and a rewritten
`src/transformation/aurum_dwh/seeds/selected_features.csv`, plus a generated
`*_narrow.yaml` config for the next step.

**Worth knowing:** two independent guards refuse to let a target column
(`fwd_ret_*`, `label_*`, `fold_id`) reach the seed — a Python assertion and a dbt test.
A target in the feature list would silently teach every future model the answer.

---

## 5. Push the ranking into the warehouse

```bash
cd src/transformation/aurum_dwh
uv run --group dbt dbt seed  --select selected_features
uv run --group dbt dbt build --select mart_feature_summary
cd -
```

**What this is about:** the seed is a CSV in git — reviewable in a pull request — and
`gold.mart_feature_summary` rebuilds itself to project only the selected columns.
Order matters: the mart reads the *Postgres table*, not the CSV, so `seed` must run
before `build`.

---

## 6. Retrain on the narrowed feature set

```bash
uv run python -m src.modeling.cli train -c src/modeling/configs/lgbm_xs_excess_5d_narrow.yaml
```

Same command as step 2, different config. The only difference between the two configs
is `preprocess.allow_list` — the 40 selected features.

---

## 7 & 8. Evaluate it, then decide

```bash
uv run python -m src.modeling.cli evaluate -c <narrow config> --version <narrow version>
uv run python -m src.modeling.cli compare  -c <narrow config> \
    --version <narrow version> --baseline <full version>
```

**What this is about:** the gate. Feature selection is a *hypothesis*, not an
improvement. If the narrowed model does not match or beat the full one on holdout ICIR
**and** decile spread, the seed does not get committed and you keep the wide model.

**Output:** `comparison.json` with both sets of numbers and a `narrowed_wins` verdict.
On the 2026-09-06 run:

| | IC | ICIR | decile spread |
|---|---|---|---|
| full, 193 features | 0.0041 | 0.43 | 10 bps |
| **narrowed, 40** | **0.0076** | **0.81** | **20 bps** |

Narrowed won on both. Seed committed.

---

## 9. Backtest — does it make money?

```bash
uv run python -m src.modeling.cli backtest -c <config> --version <version>
open models/<version>/backtest/report.html
```

**What this is about:** turning a ranking into a portfolio and charging it for trading.
This is where most research pipelines lie to their author, so it is deliberately
pessimistic.

**How the book is built:** long the top decile, short the bottom decile, dollar-neutral
and equal-weight. Because the signal has a 5-day horizon, the book is split into
**five overlapping tranches** — 1/5 of capital is committed each day and held five
days. Five vintages are live at once and only one rebalances per day, so daily turnover
is ~2/5 of the book rather than 2×. That factor of five decides whether the strategy
survives costs.

**Costs.** Charged in **basis points per side** (1 bps = 0.01%), on both the buy and
the sell, covering the bid-ask spread, brokerage, and market impact. *Not* tax —
capital gains and statutory levies sit on top of everything reported here.

The report sweeps 0 / 5 / 10 / 20 bps and reports the **break-even**: the cost level
where profit reaches zero. "The strategy dies above 18 bps per side" is a more useful
statement than any single Sharpe.

**Three reality checks, all run before quoting anything:**

| Check | Question |
|---|---|
| **Randomization** (500 shuffles) | Where does the real Sharpe sit against a portfolio with the same book and *no* information? This is the p-value the equity curve hides. |
| **Signal lag** (1 day) | Delay the signal a day. Graceful decay is fine; collapse means the model is using same-bar information and there is a leak upstream. |
| **Deflated Sharpe** | Adjusts for how many configurations were tried. Picking the best of 200 manufactures Sharpe out of noise. |

**Outputs** in `models/{version}/backtest/`: `summary.json`, `yearly.csv`,
`equity_curve.csv`, `positions.parquet`, `tearsheet.png`, `report.html`.

### Reading `report.html`

It opens in a browser, works offline, follows your light/dark theme, and states its
own biases before any performance number. Panels, in order:

1. **Predicted decile vs realized return** — the headline. Ten bars, decile 1 → 10.
   A rise left-to-right *is* the claim of the model. Flat or jagged means it does not
   rank.
2. **Predicted vs realized density** — the same thing without the bucketing.
3. **Equity by cost level** — four curves. The gap between them is the cost drag; where
   they cross 1.0 is the break-even story.
4. **Rolling 63-day IC** — is the edge steady or is it one lucky stretch?
5. **Yearly returns** — the most honest chart on the page. Shows immediately whether
   this is one good year carrying the rest.
6. **Randomization null** — the histogram of no-information Sharpes with the real one
   marked. If the red line sits inside the bell, you have not proven anything.

The holdout section comes first; the evaluation folds are shown below it and labelled
as model selection, because they chose the hyperparameters and will always look better.

---

## 10. Promote

```bash
ln -sfn <winning version> models/latest
```

**What this is about:** `models/latest` is what inference reads. Today `save_run`
repoints it automatically at whatever trained last, which is not a gate — the
two-sided rule (beat the incumbent on ICIR, lose no more than 10% of decile spread,
**and** beat its net Sharpe at 10 bps) is still applied by hand. A model that predicts
better but trades worse is not an improvement.

---

## 11. Score today

```bash
uv run python -m src.modeling.cli predict -c <config> --version latest --asof 2026-09-05
```

Ranks the current universe with the promoted model. It reads `mart_features`, which by
construction has no target columns, and replays the stored feature manifest — a column
that moved or went missing raises here rather than producing confident nonsense.

**Scope:** decisions are emitted, never auto-traded.

---

## Glossary

| Term | Meaning |
|---|---|
| **bps** | Basis point, 0.01%. Here: trading cost charged per side, per trade. |
| **IC** | Per-day rank correlation between prediction and realized return. |
| **ICIR** | IC ÷ its own volatility, annualized. Consistency of the edge. |
| **Decile spread** | Top-10% return minus bottom-10% return. |
| **Turnover** | Fraction of the book replaced at a rebalance. Drives the cost drag. |
| **Break-even** | Cost per side at which profit hits zero. |
| **Sharpe** | Return ÷ volatility, annualized. Taken across the five non-overlapping tranches, never on the daily series — overlapping labels would inflate it. |
| **Holdout** | The last 24 months, never fitted on. The only honest numbers. |
| **Purge / embargo** | Gap dropped around each validation window so a 5-day label cannot leak across the boundary. |

---

## What these numbers are not

Restated because they are easy to forget once a chart looks good:

1. **Survivorship** — the universe is today's S&P 500 applied back to 2000. Every name
   in the test survived to be in the index.
2. **Point-in-time lag is approximated** (#47) — fundamentals are assumed available on
   a fixed lag, not on their true filing date.
3. **Overlapping labels** inflate anything computed on the daily series.
4. **Zero borrow cost** and universal shortability are assumed.
5. **Pre-tax.** No capital gains, no STT/stamp duty/GST.
6. **1.9 years of true out-of-sample.** Any annualized figure from it is a rate, not a
   track record — and the current model's randomization test sits at p = 0.112, which
   does not clear a significance bar.
