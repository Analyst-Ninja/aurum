# BRONZE models — what they do and why

Written for someone who knows data engineering but not finance. Every finance
term is explained the first time it appears.

Companion docs: [`silver-staging-models-rationale.md`](silver-staging-models-rationale.md),
[`silver-intermediate-models-rationale.md`](silver-intermediate-models-rationale.md).

---

## 1. What "bronze" means here

The medallion pattern splits transformation into three layers, each with one
job:

| Layer | Job | Rule of thumb |
|---|---|---|
| **BRONZE** | mirror the landing tables, faithfully | *shape* changes, **values never do** |
| **SILVER** | clean, then engineer features | values change; this is where judgement lives |
| **GOLD** | assemble consumer-ready marts | joins and reshaping only |

Bronze is the boring layer, and that is the point. If you ever have to answer
*"is this number wrong because the source is wrong, or because we broke it?"*,
bronze is the checkpoint that lets you answer without re-downloading anything.

**Bronze does exactly three things:**

1. **Deduplicate** — landing is append-only, so re-runs create repeats
2. **Type** — landing columns are text/double, whatever pandas happened to write
3. **Rename** — `"ADJ CLOSE"` becomes `adj_close`

**Bronze deliberately does NOT:** filter bad rows, adjust prices, parse date
labels, join anything, or compute anything. All of that is silver's job. The
separation is what makes both layers testable: if a bronze test fails the
ingestion is wrong, if a silver test fails our logic is wrong.

---

## 2. The upstream problem bronze exists to solve

`src/ingestion/` writes straight to Postgres tables in the `public` schema. That
writer has three properties that would poison everything downstream if bronze
did not absorb them.

### 2.1 It appends, it never upserts

Re-run yesterday's feed and yesterday's rows are written **again**. Nothing
updates in place.

This is a deliberate ingestion choice — append-only writers are simple and
crash-safe — but it means the landing table is a *log*, not a *table*. Bronze is
what turns the log back into a table.

Every landing row carries an `MD5_HASH`: a deterministic hash of the row's
natural key, built by `BaseFeed._add_write_metadata` from the `cols_for_pk`
listed in the feed's YAML config. For daily bars that's `(SYMBOL, DATE)`. Same
bar, same hash, every run — so the hash is the dedup key.

```sql
row_number() over (
    partition by "MD5_HASH"
    order by "EXECUTION_ID" desc, "RUN_DATE" desc
) as _row_num
...
where _row_num = 1
```

**Why newest wins, not oldest.** A later run can *fix* a row. When bronze was
first written, the OHLCV landing table carried 1,503 repeated hashes, and inside
some repeat groups the earlier row had a `NULL` close that a later run had
filled in. Keeping the oldest would have preserved the hole.

**`EXECUTION_ID` sorts chronologically as a plain string** because the ingestion
framework stamps it `YYYYMMDD_HHMMSSssssss`. Zero-padded, biggest unit first —
so a descending text sort is a descending time sort. No parsing needed. (This is
the same reason ISO-8601 is the only sane date format to store as text.)

> **Current state, measured today:** landing `ohlcv_1d` holds 2,941,914 rows and
> 2,941,914 distinct hashes — **zero duplicates right now**, because the table
> was reloaded from scratch. The dedup is not currently removing anything. Keep
> it anyway: the writer still appends, so duplicates reappear the moment a feed
> is re-run over a period it has already covered. This is a guard against a
> known behaviour, not a workaround for a one-time mess.

### 2.2 Financial statements need a two-column dedup key

The statement tables need a wider partition than the price tables:

```sql
-- br_ohlcv_1d
partition by "MD5_HASH"

-- br_income_stmts_quarterly and the other five statement mirrors
partition by "MD5_HASH", "CONCEPT"
```

**Why.** A price table has one row per `(symbol, date)` — one bar, one row. A
statement table has one row per **reported line item**: Apple's Q3 2024 filing
produces ~90 rows, one for revenue, one for net income, one for inventory, and
so on. Those rows share a `(SYMBOL, QTR)` natural key, so they share an
`MD5_HASH`. Partitioning on the hash alone would collapse a whole quarterly
filing down to **one arbitrary line item** and silently discard the other 89.

`CONCEPT` is the line-item identifier (more on it in the staging doc). Adding it
restores the real grain.

*This is the kind of bug that produces a model that runs clean and is 98%
empty.* Worth staring at whenever you add a new statement source.

Verified today: `income_stmts_quarterly` has 245,320 rows and 245,320 distinct
`(MD5_HASH, CONCEPT)` pairs — the grain holds exactly.

### 2.3 Types are whatever pandas guessed

Landing stores prices as `double precision` and `RUN_DATE` as **text**. Bronze
casts both.

**Why `numeric` and not `double precision` for prices.** `double` is binary
floating point: `0.1 + 0.2 != 0.3`. Fine for one number, not fine once you build
returns, then rolling averages of returns, then ratios of those — errors
compound at every step. `numeric` is exact decimal arithmetic. It is slower, and
that is an acceptable price for prices.

**Why `RUN_DATE` needs an explicit cast.** It is text in landing, so
`where "RUN_DATE" >= '2026-01-01'` would compare *strings*. That happens to work
for ISO dates and breaks silently for anything else. Every bronze model and the
source freshness config cast it: `"RUN_DATE"::date`.

---

## 3. The incremental strategy, and why it looks over-engineered

Every bronze model uses:

```sql
{{ config(
    materialized='incremental',
    unique_key='md5_hash',
    incremental_strategy='delete+insert'
) }}
```

with this filter:

```sql
{% if is_incremental() %}
where "RUN_DATE"::date >= (
    select coalesce(max(run_date), '1900-01-01'::date)
         - interval '{{ var("window_lookback_days") }} days'
    from {{ this }}
)
{% endif %}
```

Three decisions in there, each with a reason.

### 3.1 Filter on `RUN_DATE`, not on `DATE`

This is the subtle one, and the one most likely to be "simplified" by a future
reader.

- `DATE` = the day the bar happened (business time)
- `RUN_DATE` = the day we downloaded it (ingest time)

They are usually close, but **a backfill breaks the correlation**: re-download
January 2024 today, and rows with `DATE = 2024-01-15` arrive with
`RUN_DATE = 2026-09-05`.

Filter on `DATE` and those corrections sit outside any recent window — you would
never pick them up. Filter on `RUN_DATE` and you catch **everything that
arrived recently, regardless of what period it describes**.

If you have built a CDC pipeline this is the familiar distinction between event
time and ingestion time. Watermark on ingestion time; the whole point of a
backfill is that it violates event-time ordering.

### 3.2 `delete+insert`, not `append`

`append` would add the re-read rows *next to* the existing ones, recreating the
duplicates bronze exists to remove. `delete+insert` removes every row whose
`md5_hash` appears in the new batch, then inserts the new batch. Re-running a
load is a no-op instead of a corruption. That property is **idempotency**, and
it is the reason you can safely re-run a failed DAG without thinking.

### 3.3 A lookback window, not "everything since last time"

`window_lookback_days` (currently 900, set in `dbt_project.yml`) means each run
re-reads a wide slab of history rather than only what is strictly new.

For bronze this is cheap insurance against late-arriving data. For the silver
feature models the same variable does much heavier lifting and has a subtle
failure mode — that story is in the
[intermediate models doc](silver-intermediate-models-rationale.md#the-incremental-lookback-the-hardest-part-of-this-layer).

> Bronze has **no rolling-window features**, so the warm-up problem described
> there does not apply here. Bronze rows are independent: one landing row in,
> one bronze row out. Nothing depends on its neighbours.

---

## 4. The eight models

All eight are the same file with a different source and column list.
`br_ohlcv_1d.sql` is marked in-repo as the reference model — read that one, and
the rest follow.

| Model | Source table | Grain | Dedup partition |
|---|---|---|---|
| `br_ohlcv_1d` | `ohlcv_1d` | symbol × day | `md5_hash` |
| `br_ohlcv_1min` | `ohlcv_1min` | symbol × minute | `md5_hash` |
| `br_income_stmts_quarterly` | `income_stmts_quarterly` | symbol × quarter × line item | `md5_hash, concept` |
| `br_income_stmts_yearly` | `income_stmts_yearly` | symbol × year × line item | `md5_hash, concept` |
| `br_cashflow_stmts_quarterly` | `cashflow_stmts_quarterly` | symbol × quarter × line item | `md5_hash, concept` |
| `br_cashflow_stmts_yearly` | `cashflow_stmts_yearly` | symbol × year × line item | `md5_hash, concept` |
| `br_balance_sheet_stmts_quarterly` | `balance_sheet_stmts_quarterly` | symbol × quarter × line item | `md5_hash, concept` |
| `br_balance_sheet_stmts_yearly` | `balance_sheet_stmts_yearly` | symbol × year × line item | `md5_hash, concept` |

### Why six statement tables instead of one

Because ingestion writes six. There are three **financial statements**, each
answering a different question, and each arrives quarterly and annually:

- **Income statement** — *did we make a profit over this period?* Revenue,
  costs, net income.
- **Cash flow statement** — *did cash actually move over this period?* Profit
  and cash are not the same thing; a company can be profitable on paper and run
  out of money.
- **Balance sheet** — *what do we own and owe, right now?* Assets, debts,
  equity.

Bronze mirrors the sources 1:1 and does not unify them. The union happens once,
in `stg_financials_long`, where the three-way difference can be expressed as a
column rather than as a table name.

---

## 5. Gotchas

**Landing identifiers are uppercase and must stay quoted.** The ingestion
framework writes `SYMBOL`, `DATE`, `ADJ CLOSE`. Postgres folds unquoted
identifiers to lowercase, so bare `DATE` resolves to `date` — which does not
exist. Some columns genuinely contain spaces. Hence `"ADJ CLOSE"::numeric`.

**This bites in `_sources.yml` too.** Column names there are written
`'"SYMBOL"'` — single quotes for YAML, double quotes preserved for the generated
SQL. Drop the inner quotes and tests fail with `column "date" does not exist`.

**`loaded_at_field` needs the cast.** Source freshness is configured as
`"\"RUN_DATE\"::timestamp"` because the column is text.

**Nothing outside bronze may read the `landing` source.** Enforced by
convention, stated in `_sources.yml`. If a silver model reached past bronze it
would see duplicates and text types, and the dedup rule would exist in two
places — which means it would eventually exist in two *different* places.

---

## 6. How to verify bronze is behaving

```bash
cd src/transformation/aurum_dwh

# build + test the layer
uv run --group dbt dbt build --select bronze

# is landing actually duplicate-free right now?
uv run --group dbt dbt show --inline "
  select count(*) rows, count(distinct \"MD5_HASH\") hashes
  from public.ohlcv_1d"

# bronze should equal the DISTINCT landing count, never the raw count
uv run --group dbt dbt show --inline "select count(*) from bronze.br_ohlcv_1d"
```

If `rows > hashes` and `br_ohlcv_1d` equals `hashes`, dedup is doing its job.
Today all three numbers are 2,941,914 — no duplicates present to remove.
