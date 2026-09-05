# Training and retraining — folds, fitting, the registry, and when to do it again

> **Design — not yet built.** Specifies `src/modeling/models/`, `src/modeling/data/splits.py`, the
> registry and the container. Implemented under
> [#52](https://github.com/Analyst-Ninja/aurum/issues/52),
> [#53](https://github.com/Analyst-Ninja/aurum/issues/53) and
> [#57](https://github.com/Analyst-Ninja/aurum/issues/57).
> Entry point: [`modeling-design.md`](modeling-design.md).

Contents:

1. [The problem that breaks naive validation](#1-the-problem-that-breaks-naive-validation)
2. [Purged, embargoed walk-forward](#2-purged-embargoed-walk-forward)
3. [The fold schedule](#3-the-fold-schedule)
4. [Hyperparameters](#4-hyperparameters)
5. [Early stopping on IC, not L2](#5-early-stopping-on-ic-not-l2)
6. [The registry](#6-the-registry)
7. [Retraining](#7-retraining)
8. [Promotion](#8-promotion)
9. [Running it](#9-running-it)
10. [Known gaps](#10-known-gaps)

---

## 1. The problem that breaks naive validation

`_gold_models.yml` documents the intended split as: train on `fold_id <= k`, validate on
`fold_id = k + 1`. That is the right *shape* and the wrong *implementation*, for one reason.

**The target is a five-day forward return computed on daily bars.** The row for AAPL on
2020-03-10 has a label that depends on prices through 2020-03-17. Consecutive rows share four of
their five label days.

Two consequences, both silent:

**1. Effective sample size is roughly one fifth of the row count.** 2.9M rows contain closer to
580k independent observations. A confidence interval computed on 2.9M is a fiction, and it will be
narrow enough to make noise look decisive.

**2. Adjacent folds leak.** If validation starts 2020-04-01, a training row dated 2020-03-30 has a
label extending to 2020-04-06 — inside the validation window. The model is trained on partial
knowledge of what it will be scored on. The resulting out-of-sample score is not out of sample.

This is not a small effect at a five-day horizon with monthly folds, and it flatters the model in
exactly the direction that makes you ship it.

---

## 2. Purged, embargoed walk-forward

The standard fix (López de Prado, *Advances in Financial Machine Learning*, ch. 7):

**Purge.** Drop every training row whose label window `[t, t + horizon]` overlaps the validation
window. At a five-day horizon that removes the last five trading days of training data before each
validation window.

**Embargo.** Additionally drop training rows within `E` trading days *after* the validation window
ends. Default `E = 10` — the horizon plus a buffer.

The embargo is the part that gets skipped, because at first glance training on data *after*
validation looks obviously fine. Under an **expanding** window it is not: fold 8's training set
includes the period immediately following fold 5's validation window, and serial correlation in
returns and in the features themselves carries information backwards across that boundary. The
buffer costs a few days of data and removes the argument.

```
                     ← training (expanding) →      ┊purge┊ VALIDATION ┊embargo┊  → future
   ──────────────────────────────────────────────────╳╳╳╳╳┊██████████┊╳╳╳╳╳╳╳┊──────────
                                                     5 days              10 days
```

**Both operate on dates, not `fold_id`.** `fold_id` is `dense_rank()` over
`date_trunc('month', date)` — a convenience index, one per calendar month, 321 in total. Purging by
fold would drop a whole month to remove five days.

---

## 3. The fold schedule

321 monthly folds. Do **not** fit 321 models.

| Range | Months | Role |
|---|---|---|
| 1 – 120 | ~2000-01 → 2009-12 | **Burn-in.** Training only, never evaluated. Gives the first evaluated fold a decade of history |
| 121 – 297 | ~2010-01 → 2024-09 | **Evaluation.** Refit every 12 folds (~15 fits); each fitted model predicts the following 12 months out of sample |
| 298 – 321 | ~2024-10 → 2026-09 | **Final holdout.** Touched exactly once, after hyperparameters and the feature set are frozen |

**Annual refit rather than monthly** is a cost decision, and the trade-off is explicit: monthly
refitting gives every prediction the freshest possible model but costs 177 fits per experiment and
makes the walk-forward the bottleneck on iteration. Annual costs ~15 fits and means the December
prediction comes from a model fitted the previous January. `refit_every` is a config knob; if the
drift monitoring in §7 shows within-year decay, lower it.

**The holdout is enforced, not honoured.** The split generator refuses to yield folds 298–321
unless explicitly unlocked. Every headline number in `metrics.json` and in any report comes from
there; fold-level numbers from 121–297 are model-selection artifacts and are labelled as such.

**Expanding, not rolling.** Each refit trains on everything from the start of the panel. A
low signal-to-noise problem wants data. Optional half-life decay weights (default 3 years, off)
handle regime staleness without discarding history outright. A 10-year rolling window is the
documented alternative — it is a config change, not a rewrite.

---

## 4. Hyperparameters

Cross-sectional return targets have an R² in the low single-digit basis points. Library defaults,
tuned on clean data, will fit noise immediately on 2.7M rows. The grid is small, hand-specified, and
regularizes hard. **Optuna is deliberately deferred** — a large search over a noisy objective
manufactures overfitting and inflates `n_configs_tried`, which then deflates every Sharpe you
report.

| Param | Range | Why |
|---|---|---|
| `objective` | `regression` (L2) | On the per-date standardized target |
| `num_leaves` | 15 – 63 | Small. Deep interactions are noise here |
| `min_child_samples` | 500 – 5000 | **Very large.** The default of 20 lets a leaf fit 20 rows out of 2.7M |
| `learning_rate` | 0.01 – 0.05 | Slow, with many rounds |
| `n_estimators` | ≤ 3000 | Early stopping decides |
| `feature_fraction` | 0.3 – 0.6 | The `_z`/`_decile`/`_vs_sector` triplets are heavily collinear; sampling columns decorrelates the trees |
| `bagging_fraction` / `bagging_freq` | 0.7 / 1 | |
| `lambda_l2` | 1 – 50 | |

Selection uses folds 121–297 only. `n_configs_tried` is recorded in `metadata.json`.

---

## 5. Early stopping on IC, not L2

**The default stops on the wrong thing.** L2 on a return target is dominated by the tails — a
handful of earnings gaps and crisis days. A model early-stopped on validation L2 is tuned to
predict outliers. What gets traded is the *ordering*, and the two objectives diverge.

The custom eval function is **mean per-date Spearman rank correlation** on the validation fold —
the same IC reported in [`modeling-design.md`](modeling-design.md) §5.1.

Implementation note: over ~6,600 dates × ~450 names, rank within date then take a vectorized
Pearson correlation on the ranks. Looping `scipy.stats.spearmanr` per date makes early stopping
cost more than the fit.

---

## 6. The registry

Flat files. MLflow is deferred per spec §7 Q5 — adopt it when retraining cadence makes a UI worth a
running service.

```
models/{version}/                 version = {YYYYMMDD}-{git short sha}, e.g. 20260905-a9b91fe
├── model.txt                     LightGBM native format
├── metadata.json                 git_sha · dbt manifest hash · target · config hash ·
│                                 train/val/test windows · fold spec · hyperparams ·
│                                 package versions · row and column counts · n_configs_tried
├── feature_manifest.json         ordered feature names, dtypes, NaN policy
├── preprocess_manifest.json      filters applied, thresholds, per-filter drop counts
├── metrics.json                  per-fold and aggregate IC / ICIR / decile spread / Sharpe,
│                                 the four baselines, sector and regime breakdowns
├── shap/
│   ├── ranking.csv               every feature: mean|SHAP|, std across folds, cluster id, selected
│   ├── per_fold.csv
│   └── summary.png
└── backtest/
    ├── summary.json              net Sharpe by cost level · break-even bps · capacity ·
    │                             attribution intercept · randomization p-value
    ├── yearly.csv
    ├── equity_curve.csv
    ├── positions.parquet
    └── tearsheet.png

models/latest -> models/{version}/
```

**`model.txt`, not `model.pkl`.** A pickled sklearn wrapper is bound to the exact library versions
that created it; LightGBM's native text format is portable across them. This repo will outlive its
current lightgbm pin.

**What is committed, and why it is split.** `.gitignore` excludes `models/*/model.txt`,
`models/*/backtest/positions.parquet` and `data/`. `metadata.json`, `metrics.json`,
`shap/ranking.csv` and `backtest/summary.json` **are committed** — so the run history is reviewable
as a git diff, and a performance regression shows up in a pull request rather than in a directory
nobody opens. This is the same reasoning that made `selected_features.csv` a seed rather than a
table.

---

## 7. Retraining

Three triggers, three different responses. Conflating them is how a model quietly rots: a scheduled
refit that silently repairs a drift symptom hides the fact that something broke.

| Trigger | Condition | Response |
|---|---|---|
| **Scheduled** | Monthly, after the month-end `dbt build` | Full refit on the expanded window |
| **Drift** | Rolling 63-day IC below the 25th percentile of the training-era IC distribution, **or** negative mean IC for three consecutive months | Refit **and investigate.** Treat as an incident, not a routine |
| **Structural** | The dbt manifest hash, `concept_map.csv`, `selected_features.csv`, or a `fundamental_lag_days_*` var changed | Forced refit. Detected by comparing hashes against `metadata.json` |

### 7.1 Refit cadence is not re-selection cadence

**Refit monthly. Re-run SHAP feature selection quarterly.**

If the feature set churns every month, a month-over-month change in performance is unattributable —
you cannot tell whether the model improved, the data improved, or the inputs simply changed
underneath. Holding the feature set fixed for a quarter makes the monthly series interpretable.

### 7.2 No warm starting

Every refit reruns the full walk-forward from the burn-in boundary. Warm-starting from the previous
model is faster and makes the reported out-of-sample history a fiction: the "out-of-sample"
predictions would come from a model that had already seen those rows in a previous incarnation.

### 7.3 Backtest on every refit

Each scheduled refit reruns the full backtest and appends its `summary.json` to a running history,
so strategy decay is visible as a series rather than discovered at the next review. Predictive
decay (IC) and economic decay (net Sharpe) do not move together — costs and turnover can eat a
signal that still ranks fine.

---

## 8. Promotion

A new run replaces `models/latest` only if, on the holdout, **all three** hold:

1. ICIR ≥ incumbent's,
2. decile spread ≥ 90% of incumbent's,
3. backtested **net Sharpe at 10 bps per side** ≥ incumbent's.

Criterion 3 is the one that makes the gate two-sided. A model that predicts better but trades worse
— higher turnover, worse capacity, concentration in illiquid names — is not an improvement, and
criteria 1 and 2 cannot see it. See [`backtesting.md`](backtesting.md).

**A rejected run is kept**, with its metrics. A failed experiment that leaves no record gets rerun.

---

## 9. Running it

Config drives everything, mirroring the ingestion framework — one YAML fully specifies a run and
nothing is wired in Python.

```bash
uv sync --group modeling

uv run python -m src.modeling.cli train           -c src/modeling/configs/lgbm_xs_excess_5d.yaml
uv run python -m src.modeling.cli evaluate        -c src/modeling/configs/lgbm_xs_excess_5d.yaml --version 20260905-a9b91fe
uv run python -m src.modeling.cli select-features -c src/modeling/configs/lgbm_xs_excess_5d.yaml --version 20260905-a9b91fe
uv run python -m src.modeling.cli backtest        -c src/modeling/configs/lgbm_xs_excess_5d.yaml --version latest
uv run python -m src.modeling.cli predict         -c src/modeling/configs/lgbm_xs_excess_5d.yaml --version latest --asof 2026-09-05
```

### 9.1 Container

Training only. No Airflow, no MLflow service, no serving — those are Phases 7 and 8.

```bash
docker compose -f docker-compose.modeling.yml run --rm trainer \
  train -c src/modeling/configs/lgbm_xs_excess_5d.yaml
```

`docker/modeling.Dockerfile` — `python:3.12-slim` matching `.python-version`, uv,
`uv sync --locked --group modeling`, non-root user. The compose service mounts `./models` and
`./data` so artifacts survive the container, and reaches the **host's** Postgres via
`host.docker.internal` (`network_mode: host` on Linux). Standing up a database service inside
compose would mean loading 2.9M rows into it; for a local-first project, reaching the host is the
honest simplification.

### 9.2 Dependencies, and one rule amended

New `modeling` dependency group: `lightgbm`, `shap`, `scikit-learn`, `numpy`, `scipy`, `pyarrow`,
`matplotlib`.

This contradicts the rule in `CLAUDE.md` — *"Anything importable by `src/` belongs in
`[project].dependencies`; tooling belongs in a group"* — because `src/modeling/` genuinely imports
these. **The rule is amended rather than followed**: *anything importable by the default runtime
path (`src/ingestion`, `main.py`) belongs in `[project].dependencies`; optional subsystems get
their own group.* Putting a ~400 MB ML stack into the ingestion runtime to satisfy a wording is the
worse trade.

Note the reason the original rule existed does **not** apply here. `dbt-core` was excluded from the
default sync because it pulls `dbt-core-experimental-parser`, an sdist with no wheel, which cannot
install under CI's `--no-build`. Every modeling dependency publishes manylinux wheels, so
`uv sync --locked --no-build --group modeling` works and CI keeps `--no-build`.

---

## 10. Known gaps

| # | Gap | Cost | Fix |
|---|---|---|---|
| 1 | Annual refit means late-in-year predictions come from an 11-month-old model | Understates achievable performance if drift is fast | Lower `refit_every`; the drift monitor in §7 will show whether it matters |
| 2 | The hyperparameter grid is hand-specified and small | Probably leaves performance on the table | Optuna with a *pre-registered* budget, once the baseline is trustworthy |
| 3 | Drift thresholds (25th percentile, three months) are assumed, not calibrated | Will fire too often or too rarely at first | Calibrate against the first year of monitoring |
| 4 | The structural trigger detects a dbt manifest change but not *what* changed | Every warehouse edit forces a full refit | Diff the manifest by model |
| 5 | No automated orchestration — refits are run by hand | Cadence depends on someone remembering | Airflow `aurum_retrain` DAG, spec §3.5, Phase 7+ |

---

## See also

| Doc | Content |
|---|---|
| [`modeling-design.md`](modeling-design.md) | Target and model choice, metrics, limitations |
| [`preprocessing-contract.md`](preprocessing-contract.md) | What reaches the model, and the inference symmetry contract |
| [`backtesting.md`](backtesting.md) | The net-Sharpe criterion in the promotion gate |
| [`feature-selection-shap.md`](feature-selection-shap.md) | The quarterly re-selection loop |
| [`../operations/cicd.md`](../operations/cicd.md) | CI, and the pytest step this phase re-enables |
| [`../architecture/TECHNICAL_SPEC.md`](../architecture/TECHNICAL_SPEC.md) | §3.5 `aurum_retrain`, §3.7, §7 Q5 |
