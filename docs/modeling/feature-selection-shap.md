# SHAP feature selection — the producer side of the loop

> **Design — not yet built.** Specifies `src/modeling/explain/`, implemented under
> [#55](https://github.com/Analyst-Ninja/aurum/issues/55).
>
> This is the **producer**. The consumer — the seed file's schema, why it is a seed rather than a
> table, and how `mart_feature_summary` reads it — is
> [`../warehouse/rationale/selected-features-seed.md`](../warehouse/rationale/selected-features-seed.md),
> which is built and shipped. Read that one first if you are changing the seed; read this one if
> you are changing what writes it.

Contents:

1. [The loop, and which half is missing](#1-the-loop-and-which-half-is-missing)
2. [What SHAP is, briefly](#2-what-shap-is-briefly)
3. [Computing it — four decisions](#3-computing-it--four-decisions)
4. [The cutoff](#4-the-cutoff)
5. [Guardrails](#5-guardrails)
6. [Step 8: the comparison that makes it real](#6-step-8-the-comparison-that-makes-it-real)
7. [Known gaps](#7-known-gaps)

---

## 1. The loop, and which half is missing

```
mart_training_set ──train──▶ model ──SHAP──▶ ranking ──▶ seeds/selected_features.csv
        ▲                                                            │
        │                                                        dbt seed
        │                                                            ▼
        └──────── retrain on narrowed set ◀──── gold.mart_feature_summary
```

**The right half exists.** `mart_feature_summary` is built, tested and shipped. It reads the seed,
intersects the requested names against `mart_training_set`'s real columns, and projects them
alongside a fixed key set. It has a fallback to the full panel if nothing matches.

**The left half does not.** `seeds/selected_features.csv` holds a single placeholder row written in
Phase 0 purely so the model would compile:

```csv
feature_name,rank,mean_abs_shap,selected,model_version
ret_21d,1,0.0,true,placeholder
```

`ret_21d` was chosen because it needs only price history and would therefore survive every
subsequent phase's renames. `mean_abs_shap = 0.0` is honest — no SHAP value has ever been computed
in this repo — and the mart does not read that column anyway.

This document specifies what replaces it.

---

## 2. What SHAP is, briefly

A SHAP value answers, for **one prediction and one feature**: how much did this feature move the
prediction away from the model's average output? It comes from cooperative game theory — the
feature's Shapley value in the game of "producing this prediction" — which gives it a property
ordinary tree feature-importance lacks: the values for a single prediction **sum exactly** to the
gap between that prediction and the baseline.

Averaging the absolute SHAP value of a feature across many rows gives **mean |SHAP|**: a magnitude
of influence, in the units of the prediction. That is the ranking statistic.

Why it matters that the model is a tree: `shap.TreeExplainer` computes these values **exactly**, in
polynomial time. For any other model class you would use KernelSHAP, which is a sampling
approximation and orders of magnitude slower over 200+ columns. The GOLD layer's design already
assumes tree SHAP; this is one of the reasons LightGBM is the model
([`modeling-design.md`](modeling-design.md) §3).

---

## 3. Computing it — four decisions

### 3.1 Sample, do not compute everything

`shap.TreeExplainer` over a **date-stratified sample of ~200k rows**, spread evenly across
evaluation dates.

Full SHAP over 2.7M × 200+ columns is hours of compute for a result that is used only to *order*
features. The ranking stabilizes long before the sample is exhausted. Stratifying by date rather
than sampling uniformly matters: uniform sampling over-weights recent years, because the universe
grows from ~320 names per date in 2000 to ~500 in 2026.

### 3.2 Per fold, then aggregate — and keep the spread

Compute SHAP separately for each of the ~15 walk-forward refits, then rank by
`mean(mean|SHAP|)` across folds.

**Also record the standard deviation across folds.** A feature with high mean importance and high
cross-fold variance was important *in one era*. That is not the same thing as an important feature,
and the mean alone cannot distinguish them. The per-fold detail lives in
`models/{version}/shap/per_fold.csv`; the seed schema is unchanged.

### 3.3 Prune collinear features before the cutoff

SHAP **splits credit among correlated features**. `ret_21d`, `ret_21d_z` and `ret_21d_decile`
encode nearly the same information; a tree ensemble picks among them arbitrarily at each split, so
each ends up with a third of the importance and all three may fall below a cutoff that the
underlying signal deserves to clear.

So: cluster features at |ρ| > 0.95, keep the highest-SHAP member of each cluster, and record the
cluster id per feature in `ranking.csv`. The dropped members stay in the file with
`selected = false`, so the decision is visible and reversible.

This matters more here than in a typical dataset, because GOLD deliberately ships 36 features in
three cross-sectional variants each — collinearity is built in by design, not by accident.

### 3.4 Compute on the right target

On the raw `fwd_ret_5d` target, a Phase 4 end-to-end run produced a ranking topped by
`market_vol_63d`, `market_xs_dispersion`, `market_vol_21d`, `market_ret_21d` and `month_of_year` —
all market-level columns, identical across every symbol on a date, carrying exactly zero
cross-sectional information.

> **If a SHAP ranking comes back dominated by market-level or calendar columns, the target is
> wrong, not the ranking.** Train on `fwd_ret_5d_excess`.

This is the measured evidence behind the target choice in
[`modeling-design.md`](modeling-design.md) §2.1, and it is the single most useful diagnostic in
this document.

---

## 4. The cutoff

**Cumulative 95% of total mean |SHAP|, capped at top-40.** Record which rule bound.

Three approaches, in preference order, all defensible; what is not defensible is failing to record
which was used:

| Rule | When |
|---|---|
| **Cumulative share** — keep features until 90–95% of total mean\|SHAP\| is covered | Default. Adapts to how concentrated the importance actually is |
| **Fixed count** — top 30–40 | When the cumulative rule produces an unstable count run to run |
| **Absolute floor** — mean\|SHAP\| above a threshold | Rarely. The threshold is regime-dependent and goes stale |

The cap exists because the cumulative rule degenerates when importance is flat: 150 features each
contributing 0.6% would all clear a 95% bar, and a "selection" of 150 out of 200 is not a
selection.

The file keeps **every** feature ranked, with `selected = true` only above the cutoff. Truncating it
to the selected set discards exactly the information needed to retune the threshold later.

---

## 5. Guardrails

**Targets must never reach the ranking.** Filter `fwd_ret_*`, `label_*` and `fold_id` out *before*
writing the CSV, and assert it. A target that reaches the seed makes `mart_feature_summary` project
the answer as a feature, and every model trained on it afterwards is silently perfect and
completely useless.

Two layers, deliberately redundant:

1. A Python assertion in `seed_writer.py`, covered by a unit test that deliberately passes one in.
2. **A new dbt test, `tests/assert_no_targets_in_feature_summary.sql`** — fails if any column of
   `mart_feature_summary` outside the declared key set matches `fwd_ret%` or `label%`. This test is
   called for in `selected-features-seed.md` §7 and has never been written; it is part of
   [#55](https://github.com/Analyst-Ninja/aurum/issues/55).

**Never let the seed reach zero selected rows.** `mart_feature_summary` compiles to `select  from
…` and fails with a bare SQL syntax error that names nothing useful.

**`dbt seed` before `dbt run`.** The model reads the seeded Postgres table, not the CSV on disk.
Editing the file and running the model gets you the previous selection with no warning.

```bash
cd src/transformation/aurum_dwh
uv run --group dbt dbt seed --select selected_features
uv run --group dbt dbt build --select mart_feature_summary
```

**The human checkpoint is the point.** The seed is committed to git, so the PR diff shows which
features entered, which left, and how the SHAP values moved. A bad training run — undertrained,
leaking, or trained on the wrong target — produces a nonsense ranking, and a commit review catches
it before it reshapes the mart. The cost is that retraining is not fully automated. That is the
intended trade.

---

## 6. Step 8: the comparison that makes it real

> **Feature selection is a hypothesis, not an improvement.**

After writing the seed and rebuilding the mart: **retrain on the narrowed set and compare against
the full-feature baseline on the holdout**, on ICIR and decile spread.

If the narrowed model does not match or beat the baseline, **the seed is not committed**. Dropping
160 features is only worth doing if it costs nothing — and the reasons it might help (less noise,
faster fits, a model you can reason about) are hypotheses that this comparison tests.

Both results go in `metrics.json`. A narrowed model that loses is a recorded finding, not a
discarded run.

---

## 7. Known gaps

| # | Gap | Cost | Fix |
|---|---|---|---|
| 1 | Mean \|SHAP\| measures influence on the *prediction*, not on realized returns | A feature the model leans on heavily and wrongly ranks high | Cross-check the top of the ranking against single-factor IC |
| 2 | The 0.95 correlation threshold is assumed, not tuned | Clusters may be too coarse or too fine | Sensitivity check at 0.90 / 0.95 / 0.99 on the first real run |
| 3 | Selection runs on the primary target only | The classification and ranking heads may want different features | Re-run per head once §6 has a baseline |
| 4 | Quarterly re-selection cadence is a judgement call | Feature set may go stale between runs, or churn needlessly | Calibrate against observed drift |
| 5 | SHAP is computed on a 200k sample | Ranking has sampling error, unquantified | Report a bootstrap interval on mean\|SHAP\| |

---

## See also

| Doc | Content |
|---|---|
| [`../warehouse/rationale/selected-features-seed.md`](../warehouse/rationale/selected-features-seed.md) | The seed itself: schema, why a seed, the gotchas table |
| [`modeling-design.md`](modeling-design.md) | Why the target choice determines what SHAP can see |
| [`training-and-retraining.md`](training-and-retraining.md) | §7.1 — why re-selection is quarterly while refit is monthly |
| [`../warehouse/rationale/gold-models-rationale.md`](../warehouse/rationale/gold-models-rationale.md) | §6 — how `mart_feature_summary` consumes the seed, as built |
| [`../architecture/TECHNICAL_SPEC.md`](../architecture/TECHNICAL_SPEC.md) | §3.7 step 3 |
