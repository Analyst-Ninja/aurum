# selected_features — the SHAP feature-selection loop

> Companion to `src/transformation/aurum_dwh/seeds/selected_features.csv`.
> Created 2026-09-03 with a single placeholder row; **replaced with a real SHAP ranking on
> 2026-09-06** by [#55](https://github.com/Analyst-Ninja/aurum/issues/55). It now carries all 193
> features of `mart_training_set`, ranked, with 40 selected — the `cap` rule bound, not the
> cumulative-share rule. The producer is `src/modeling/explain/`.
>
> **This document is the consumer side** — the seed's schema, why it is a seed rather than a table,
> and how `mart_feature_summary` reads it. The **producer** — what computes the SHAP ranking and
> writes this file — is [`../../modeling/feature-selection-shap.md`](../../modeling/feature-selection-shap.md).
>
> Two corrections since this was written. The phase was renumbered: the SHAP loop belongs to
> **Phase 6 (modelling)**, not Phase 4 (#36), which shipped the GOLD marts that the loop reads.
> And §2 below describes the compile-time `run_query` mechanism as designed; the **shipped**
> `mart_feature_summary` uses `load_relation` plus an intersection against `mart_training_set`'s
> real columns, with a fallback to the full panel when nothing matches — see
> [`gold-models-rationale.md`](gold-models-rationale.md) §6 for the as-built version. The failure
> modes described in §2 are unchanged and still apply.

---

## 1. What this seed is for

`docs/architecture/TECHNICAL_SPEC.md` §3.7 describes a feedback loop: train a model on GOLD features, compute SHAP values, prune the low-importance ones, and materialise the surviving set as `mart_feature_summary` — the narrowed view used for retraining.

This seed **is** that loop's state. It carries the SHAP ranking from the last training run, and `mart_feature_summary` builds itself from whatever it contains.

```
mart_training_set  ──(train)──▶  model  ──(SHAP)──▶  ranking
                                                        │
                                        selected_features.csv
                                                        │
                                                        ▼
                                              mart_feature_summary
                                                        │
                                                        └──(retrain)──▶ …
```

Regenerating the mart after a training run is:

```bash
uv run --group dbt dbt seed --select selected_features
uv run --group dbt dbt run  --select mart_feature_summary
```

That is the entire loop. No orchestration, no registry.

---

## 2. Why the file must exist *before* any model is trained

This is the non-obvious part, and the reason the seed ships with a placeholder rather than being deferred to Phase 4.

`mart_feature_summary` selects a column list that changes every training run. dbt models are static SQL compiled ahead of execution, so the only dbt-native way to express this is to read the seed **at compile time**:

```sql
{%- set q %}
  select feature_name from {{ ref('selected_features') }}
  where selected order by rank
{% endset -%}

{%- if execute -%}
  {%- set cols = run_query(q).columns[0].values() -%}
{%- else -%}
  {%- set cols = [] -%}
{%- endif -%}

select symbol, date, {{ cols | join(', ') }}
from {{ ref('mart_training_set') }}
```

Verified against the real seed — this compiles to:

```sql
select symbol, date, ret_21d from ...
```

Two consequences follow, and both bite silently.

### `run_query` hits the database, not the CSV

The seed **table** must already exist in Postgres when the model compiles. So `dbt seed` must always precede `dbt run`. A fresh clone that goes straight to `dbt run` fails with a confusing error about a missing relation, in a model whose SQL contains no obvious reference to it.

### An empty result produces broken SQL, not a useful error

If the query returns zero rows, `cols` is empty and `cols | join(', ')` yields an empty string. Tested:

```sql
select  from (select 1 as x) t
```

That is a syntax error at run time — no "empty feature list" message, no failed test, just malformed SQL. The placeholder row is therefore **load-bearing**, not decorative. It guarantees the model compiles to something valid from the very first build.

> If you take one thing from this document: never let this seed reach zero selected rows.

---

## 3. Schema

```csv
feature_name,rank,mean_abs_shap,selected,model_version
ret_21d,1,0.0,true,placeholder
```

| Column | Type | Purpose |
|---|---|---|
| `feature_name` | text | Column name in `mart_training_set`. Must match exactly — a typo silently drops a feature or breaks compilation. |
| `rank` | integer | SHAP importance rank, 1 = most important. Drives column order and makes the file diff-readable. |
| `mean_abs_shap` | numeric | Mean absolute SHAP value. The evidence behind the rank; lets you see *how much* better rank 1 is than rank 20. |
| `selected` | boolean | **Whether this feature enters the mart.** |
| `model_version` | text | Which training run produced this ranking. |

`rank` is safe as a column name in Postgres despite being a window function — confirmed by loading and querying it.

### Why `selected` and `model_version` were added

The approved plan (now folded into `docs/warehouse/dwh-medallion.md`) originally specified `feature_name,rank,mean_abs_shap`. Both additions close real gaps.

**`selected` — because rank alone has no cutoff.** If the SHAP loop writes all ~90 ranked features, the mart selects all 90, which makes it identical to `mart_training_set` and therefore pointless. Encoding the cutoff by *truncating the file* would work but throws away the ranking of everything below the line — exactly the data you need to decide whether the cutoff was right. Keeping the full ranking with an explicit flag lets you retune the threshold without rerunning SHAP.

**`model_version` — because a selection can go stale.** Without it there is no way to tell whether `mart_feature_summary` reflects the model you are actually running or a ranking from three retrains ago. When the loop runs for real this becomes the field that answers "is this mart current?".

---

## 4. Current state and what happens next

Right now the file has one row: `ret_21d`, flagged selected, `model_version = placeholder`, `mean_abs_shap = 0.0`.

`ret_21d` was chosen deliberately — a 21-day return needs nothing but price history, so it is certain to exist in `mart_training_set` whatever else changes in Phases 2–4. A placeholder naming a feature that later gets renamed would reintroduce the compile failure this row exists to prevent.

`mean_abs_shap = 0.0` is honest: no model has run, so there is no importance to report. The value is unused by the mart, which reads only `feature_name` and `selected`.

**In Phase 4 (#36)** the training job replaces this file wholesale with the real ranking — typically 60–90 rows, of which perhaps 20–40 carry `selected = true`.

---

## 5. Why a seed rather than a table

A dbt seed is a human-maintained CSV committed to git. This one is machine-written, which is a genuine tension — the alternative would be to have the training job write a real table that dbt declares as a `source`.

The seed is the deliberate choice, for three reasons:

- **Version control.** The feature set used by any historical build is recoverable from git. With a table, the previous selection is simply overwritten.
- **Reviewability.** A pull-request diff shows exactly which features entered or left, and by how much their SHAP values moved. That is a meaningful review artifact.
- **A bad run cannot silently reshape the mart.** SHAP on an undertrained or leaking model can produce a nonsense ranking. The commit step is a checkpoint where a human notices.

The cost is that retraining is not fully automated: the loop requires a human to review and commit. That is an acceptable trade at this stage and should be revisited only if retraining becomes frequent enough for the review step to be the bottleneck.

---

## 6. The workflow

```
1. Train on mart_training_set with walk-forward folds
2. shap.TreeExplainer -> mean absolute SHAP per feature
3. Write selected_features.csv: all features ranked, cutoff applied to `selected`
4. Review the git diff - which features entered, which left, how ranks moved
5. Commit
6. dbt seed --select selected_features
7. dbt run  --select mart_feature_summary
8. Retrain on the narrowed set; compare against the full-feature baseline
```

Step 8 matters. Feature selection is a hypothesis, not an improvement — it must be measured. If the narrowed model does not match or beat the baseline on out-of-sample walk-forward folds, the cutoff was too aggressive.

### Choosing the cutoff

No fixed rule; three defensible approaches, in rough order of preference:

1. **Cumulative SHAP** — keep features covering the top 90–95% of total mean absolute SHAP. Adapts to how concentrated importance actually is.
2. **Fixed count** — keep the top N (30–40 is typical for a tabular panel). Predictable and easy to reason about.
3. **Absolute floor** — drop anything below a `mean_abs_shap` threshold. Simplest, but the threshold is regime-dependent and needs revisiting.

Whichever you use, record it — a future reader needs to know why 34 features were selected rather than 30.

---

## 7. Gotchas

| Gotcha | Consequence |
|---|---|
| Zero rows with `selected = true` | `select  from …` — a syntax error, not a helpful message. §2 |
| `dbt run` before `dbt seed` on a fresh clone | Compile failure on a missing relation, in a model that never names it |
| `feature_name` typo | Feature silently dropped, or the mart fails to compile |
| A selected feature renamed in Phase 3/4 | Same as a typo. Cross-check against `mart_training_set` columns after any feature rename |
| Committing a ranking without its `model_version` | No way to tell whether the mart is current |
| Target columns appearing in the ranking | `fwd_ret_*` and `label_*` must never be selected — they are the labels. Filter them out before writing the CSV |

That last one is worth a guard rather than a convention. `tests/assert_no_targets_in_feature_summary.sql` should fail if any `selected` feature name matches `fwd_ret%` or `label_%`.

---

## 8. References

- `docs/architecture/TECHNICAL_SPEC.md` §3.7 — modeling, SHAP selection, walk-forward validation
- `docs/warehouse/dwh-medallion.md` — the warehouse as built; `mart_feature_summary` and the SHAP loop
- Issue #36 — GOLD marts, where the real ranking gets produced
