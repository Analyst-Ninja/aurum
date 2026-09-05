# Preprocessing — the contract between the warehouse and the model

> **Design — not yet built.** Specifies `src/modeling/data/`, implemented under
> [#52](https://github.com/Analyst-Ninja/aurum/issues/52). Entry point:
> [`modeling-design.md`](modeling-design.md).

Most of what a preprocessing stage normally does — scaling, winsorizing, rank-transforming — is
already done, in SQL, per date, inside `gold.mart_features`. **This layer must not redo it.** What
remains is smaller and mostly about deciding what to throw away.

All measured numbers are from the live warehouse on **2026-09-05**, with the query that produced
each.

Contents:

1. [The one rule this layer exists to enforce](#1-the-one-rule-this-layer-exists-to-enforce)
2. [Load](#2-load)
3. [Row filters](#3-row-filters)
4. [Column policy — three deny-lists](#4-column-policy--three-deny-lists)
5. [Target transforms](#5-target-transforms)
6. [Missing values](#6-missing-values)
7. [Sample weights](#7-sample-weights)
8. [The manifests](#8-the-manifests)
9. [Known gaps](#9-known-gaps)

---

## 1. The one rule this layer exists to enforce

> **Every preprocessing step must be replayable against `gold.mart_features` alone.**

`mart_features` has no target columns — that is a structural guarantee of the GOLD layer, enforced
by `tests/assert_no_lookahead.sql`. At inference time, that is all there is: a feature row for
today, no answer.

So a preprocessing step that needs the target, or needs the rest of the training set, cannot exist.
Concretely, this rules out:

- imputing a missing value with a column mean computed over the training set,
- scaling with statistics fitted on training data,
- any filter that uses the outcome.

The one function that matters is:

```python
def build_features(df: pd.DataFrame, manifest: FeatureManifest) -> tuple[pd.DataFrame, list[str]]:
    ...
```

**Training** calls it and writes the manifest. **Inference** reads the manifest and calls it with
the same code path. Column order is pinned in the manifest; a mismatch raises rather than silently
reordering — a silently reordered feature matrix produces confident, meaningless predictions.

The target transforms in §5 are the deliberate exception, and they touch only the target, which
does not exist at inference.

---

## 2. Load

Source: `gold.mart_training_set` for training, `gold.mart_features` for inference.

- **Downcast float64 → float32 on read.** 2.9M × 228 in float64 is ~5.3 GB; float32 halves it with
  no consequence for a signal whose magnitude is basis points.
- **Cache to Parquet** under `data/training/{table}_{max_date}.parquet`, keyed on the source
  table's `max(date)`. Repeated pulls are the dominant cost of iterating on a model, and the
  warehouse only changes when dbt runs.
- Reuse `src/utils/env.load_env()` for `.env`. Credentials in config are env var **names**, not
  values, resolved at connect time — the same convention as the ingestion configs.

**Case convention differs by layer, and this is a real trap.** The ingestion framework uses
uppercase identifiers (`SYMBOL`, `DATE`); dbt models are lowercase. This layer reads dbt output, so
everything in `src/modeling/` is lowercase. Postgres identifiers are quoted in both, so the
mismatch fails loudly rather than silently — but only at query time.

---

## 3. Row filters

Applied in this order, each one counted and written to `preprocess_manifest.json`.

| # | Filter | Why | Measured cost |
|---|---|---|---|
| 1 | Drop rows with a NULL target | The last five trading dates have no five-day forward return yet — guaranteed by `tests/assert_targets_null_at_edge.sql` | **2,515 rows** (0.09%) |
| 2 | **Warm-up burn-in** — drop the first 252 bars per symbol | See §3.1 | **126,335 rows** (4.4%) |
| 3 | Drop dates with fewer than `min_cross_section` symbols (default 100) | A per-date z-score, decile or excess return over a handful of names is noise | **0 rows** — see §3.2 |

Tradability (`close_raw >= 1.0`, `adv_21d >= 1e6`) is **already applied inside
`mart_training_set`**. Do not reapply it; doing so is harmless but signals a misreading of the
model, which is worse.

```sql
-- filter 1
select count(*) from gold.mart_training_set where fwd_ret_5d_excess is null;      -- 2515

-- filter 2
with r as (select row_number() over (partition by symbol order by date) rn from gold.mart_training_set)
select count(*) filter (where rn <= 252) from r;                                  -- 126335
```

### 3.1 Why 252 bars of burn-in

252 trading days is one year. Features built on a 252-row window — `vol_252d`, `beta_252d`,
`idio_vol_252d`, `max_drawdown_252d`, `high_252d`, `low_252d`, `ret_252d` — are NULL or computed
over a partial window for a symbol's first year in the panel.

The subtle harm is not the missing values themselves; LightGBM handles those. It is that **the
missingness is correlated with something real**: early-listing rows are systematically different
from mature rows. Leave them in and the model learns "rows with a NULL `beta_252d` behave like
recently-listed companies", which is true, era-specific, and not what we asked it to learn.

Note the null rates are *low* overall — `vol_252d` is 0.03% null and `beta_252d` 3.2% across the
whole panel — precisely because the warehouse's 900-day incremental lookback warms these windows up
properly. The burn-in filter is about the small remaining head of each symbol's history.

### 3.2 Filter 3 currently drops nothing — keep it anyway

The thinnest year in the panel is 2000, and even there each date carries 311–329 symbols.

```sql
with c as (select date, count(*) n from gold.mart_training_set group by date)
select extract(year from date)::int, min(n), max(n) from c group by 1 order by 1 limit 3;
-- 2000 | 311 | 329
-- 2001 | 325 | 342
-- 2002 | 335 | 349
```

So the filter is a **guard, not an active filter** on current data. It stays because the universe is
configurable and a future run on a narrower universe would silently produce meaningless
cross-sectional statistics. The manifest logs the zero, which is the point — a guard that has never
fired should say so.

---

## 4. Column policy — three deny-lists

All three live in config and are **explicit**. Nothing is inferred from dtype or suffix.

### 4.1 Leakage

```
fwd_ret_5d, fwd_ret_21d, fwd_ret_5d_excess, fwd_ret_5d_xs_decile, label_up_5d, fold_id
```

Matched by the regex `^(fwd_ret|label_)` plus `fold_id`, and **asserted**: `build_features()` raises
if any such column reaches `X`. A unit test deliberately passes one in.

> **The `_decile` trap.** `mart_training_set` has **37** columns ending in `_decile`. Only 36 are
> features; the 37th is `fwd_ret_5d_xs_decile`, a target. Any rule that selects or drops "the
> decile columns" by suffix either leaks the answer or discards a feature, and both failures are
> silent.

### 4.2 Identifiers

`symbol` and `date` are retained as an index. Never as features. A tree given a symbol identifier
memorizes tickers.

### 4.3 Non-stationary levels — the judgement call

Dropped:

```
adj_close  close_raw  market_cap  enterprise_value  revenue  net_income  total_assets
total_equity  total_debt  shares_outstanding  adv_21d  dollar_volume
ma_10  ma_20  ma_50  ma_200  high_252d  low_252d
```

**The argument.** A tree splits on an absolute threshold. `market_cap > 5e10` selects a top-decile
firm in 2001 and a mid-cap in 2026. Trained across a 26-year panel, that split does not encode a
relationship between size and return; it encodes *when*. The model memorizes an era and generalizes
to nothing.

Every one of these has a cross-sectional counterpart in GOLD — `market_cap_z`, `market_cap_decile`,
`market_cap_vs_sector` — that expresses the same information as a position *within that day's
universe*, which is stationary by construction. Those are kept.

**What is deliberately kept in raw form**: scale-free quantities. Margins (`gross_margin`,
`net_margin`), returns (`ret_21d`, `mom_12_1`), ratios (`roe`, `roic`, `debt_to_equity`), bounded
oscillators (`rsi_14`, `bollinger_pctb_20`), volatilities (`vol_21d`), growth rates
(`revenue_growth_yoy`). A 12% net margin means the same thing in 2001 and 2026. These are kept
*alongside* their `_z` versions — the raw value carries an absolute reading and the z-score carries
a relative one, and letting the model choose is cheaper than guessing.

Market-level columns (`market_vol_63d`, `market_breadth`, `market_xs_dispersion`, …) are also
excluded from the feature set — they are identical across all symbols on a date and so carry zero
ranking information, exactly the failure mode measured in
[`modeling-design.md`](modeling-design.md) §2.1. They remain available as **regime labels for
reporting**, which is a different use.

---

## 5. Target transforms

Training only. Neither exists at inference. Both are per-date.

1. **Winsorize** `fwd_ret_5d_excess` at the 1st and 99th percentile within the date.
2. **Standardize** — divide by that date's cross-sectional standard deviation.

Rationale and the measured dispersion number are in [`modeling-design.md`](modeling-design.md) §2.3.

**One null trap, inherited from GOLD and worth restating**: Postgres `greatest`/`least` ignore
NULLs, so a naive winsorize clamps a NULL to the 1st percentile rather than leaving it NULL. The
pandas implementation must not reproduce that bug in the other direction — `clip()` propagates NaN
correctly, but any hand-rolled `np.maximum` does not.

---

## 6. Missing values

> **Keep them. Do not impute.**

Nulls here are structural and large:

| Measure | Value |
|---|---|
| Rows with no fundamental data at all | **34.6%** |
| `roe` null, among rows that *have* fundamentals | **32.6%** |
| `ebitda_ttm` null, among rows that have fundamentals | **37.7%** |
| `total_equity` null, among rows that have fundamentals | **11.2%** |
| `market_cap_z` null, whole panel | **46.8%** |

They come from two distinct causes, and the model benefits from being able to tell them apart:

- **Not yet knowable** — the symbol had not filed by that date, so the point-in-time join found
  nothing. Genuinely absent information.
- **Never mapped** — the concept exists in the filing but is not in `seeds/concept_map.csv`
  (~53% of encountered XBRL concepts are unmapped).

Imputing zero makes a missing margin indistinguishable from a genuine zero margin. Imputing a
cross-sectional median injects information from *other stocks on the same date* into a row that
should not have it — a subtle form of leakage that survives every leakage test in the repo.

Two columns let the model condition on staleness explicitly rather than confusing stale with
missing:

- `days_since_available` — already in GOLD, the gap between the fundamental's assumed-knowable date
  and the bar's date.
- `has_fundamentals` — added here, `fundamental_available_from is not null`.

---

## 7. Sample weights

Uniform by default.

Optional **half-life decay** on training rows (default 3 years, **off** by default), for the
expanding-window retraining policy in [`training-and-retraining.md`](training-and-retraining.md).
The trade-off it manages: more history helps a low signal-to-noise problem, but a 2003 regime is
less relevant than a 2025 one.

Volume weighting is available and off. It biases the fit toward mega-caps, where the signal is
weakest and the competition is fiercest.

---

## 8. The manifests

Two JSON files, written at training, read at inference.

**`preprocess_manifest.json`** — provenance:

```json
{
  "source_table": "gold.mart_training_set",
  "source_max_date": "2026-09-02",
  "rows_in": 2895171,
  "filters": [
    {"name": "null_target",      "param": "fwd_ret_5d_excess", "dropped": 2515},
    {"name": "warmup_burnin",    "param": 252,                 "dropped": 126335},
    {"name": "min_cross_section","param": 100,                 "dropped": 0}
  ],
  "rows_out": 2766321,
  "target_transforms": ["winsorize_1_99_per_date", "standardize_per_date"]
}
```

**`feature_manifest.json`** — the contract:

```json
{
  "features": ["ret_21d", "ret_21d_z", "..."],
  "dtypes":   {"ret_21d": "float32", "sector": "category"},
  "categorical": ["sector", "industry"],
  "nan_policy": "native",
  "denied": {"leakage": ["..."], "identifier": ["..."], "non_stationary": ["..."]}
}
```

`features` is **ordered**, and that order is the contract. Inference asserts an exact match.

---

## 9. Known gaps

| # | Gap | Cost | Fix |
|---|---|---|---|
| 1 | The non-stationary deny-list is a judgement call, not a measurement | Possibly discarding usable information | Ablate: train with and without the level columns, compare holdout ICIR |
| 2 | Burn-in is a fixed 252 bars for every feature, though only the 252-window family needs it | Drops 4.4% of rows, some unnecessarily | Per-feature warm-up masks, if the row loss ever matters |
| 3 | `has_fundamentals` conflates "not yet filed" with "concept unmapped" | The model cannot distinguish two different kinds of absence | Separate indicators once `concept_map.csv` coverage is measured per row |
| 4 | Parquet cache is keyed on `max(date)` only | A dbt rebuild that changes history without extending it reuses a stale cache | Key on the dbt manifest hash as well |

---

## See also

| Doc | Content |
|---|---|
| [`modeling-design.md`](modeling-design.md) | Target and model choice, metrics, limitations |
| [`training-and-retraining.md`](training-and-retraining.md) | Splits, hyperparameters, registry, retraining |
| [`../warehouse/rationale/gold-models-rationale.md`](../warehouse/rationale/gold-models-rationale.md) | The cross-sectional transform this layer relies on and must not repeat |
| [`../warehouse/data-dictionary.md`](../warehouse/data-dictionary.md) | Every column, with types and gotchas |
