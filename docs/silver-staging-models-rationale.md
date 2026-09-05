# SILVER staging models — what they do and why

Written for someone who knows data engineering but not finance. Every finance
term is explained the first time it appears.

Companion docs: [`bronze-models-rationale.md`](bronze-models-rationale.md),
[`silver-intermediate-models-rationale.md`](silver-intermediate-models-rationale.md).

---

## 1. What staging is for

Bronze mirrors the source faithfully, **including its mistakes**. Staging is the
first layer allowed to have an opinion.

Three models, three shapes of problem:

| Model | Input | Output grain | The hard part |
|---|---|---|---|
| `stg_ohlcv_daily` | `br_ohlcv_1d` | symbol × date | making a price series *comparable across time* |
| `stg_financials_long` | 6 bronze statement mirrors | symbol × statement × period × line item | making six tables into one, and dating them |
| `stg_companies` | `company_meta` seed | symbol | cleaning a web scrape |

Staging still does **no cross-row maths**. No returns, no rolling averages, no
windows that look at neighbouring days. That is a deliberate line: rolling
windows interact badly with incremental loads (see the
[intermediate doc](silver-intermediate-models-rationale.md#the-incremental-lookback-the-hardest-part-of-this-layer)),
and keeping them out of staging means the lookback problem lives in exactly one
layer instead of leaking through the whole DAG.

---

## 2. `stg_ohlcv_daily` — the price series

### 2.1 What an OHLCV bar is

One row per stock per trading day, summarising all trading that day:

| Field | Meaning |
|---|---|
| **O**pen | first traded price of the day |
| **H**igh | highest traded price |
| **L**ow | lowest traded price |
| **C**lose | last traded price |
| **V**olume | number of shares that changed hands |

Plus `adj_close`, which is where things get interesting.

### 2.2 Step one: drop bars that cannot be true

```sql
volume is not null and volume <> 0
and close > 0 and adj_close > 0 and open > 0 and high > 0 and low > 0
and high >= low
and high >= close and high >= open
and low  <= close and low  <= open
```

Two families of check:

**Impossible values.** A zero-volume bar means nothing traded — a market
holiday the data provider padded out, or a stock that was halted. It is not a
real day of trading, and leaving it in inserts a fake "price didn't move" data
point into every volatility calculation.

**Internal contradictions.** The high is *by definition* the largest price of
the day, so `high >= close` is not a heuristic — a row violating it is corrupt.
These are cheap, absolute assertions. They cost one scan and remove a class of
garbage that would otherwise show up much later as an inexplicable outlier.

`adj_close > 0` is guarded specifically because it is the **numerator** of the
adjustment factor below. A null there would silently null out every adjusted
price on the row rather than dropping the row.

> **Measured:** 2,941,914 bronze rows → 2,935,412 staged rows.
> **6,502 dropped, 0.22%.** Small enough to be plausible noise, large enough to
> be worth removing.

### 2.3 Step two: split and dividend adjustment — the important idea

This is the one finance concept in this model that genuinely matters, and it has
a clean data-engineering analogy: **the raw price series is not comparable with
itself over time.** It's like a metric whose unit changed halfway through
without the name changing.

**Stock splits.** A company whose share price has grown to 
`$500 may decide that
is awkward for small investors, and perform a **4-for-1 split**: every holder of
1 share now holds 4, and the price drops from $500 to $125 overnight.`

Nothing happened. Nobody gained or lost a cent — the same pie, cut into more
slices. But the raw price series now shows:

```
2020-08-28   $499.23
2020-08-31   $129.04     <-- looks like a 74% crash
```

Any model reading the raw series learns that Apple lost three quarters of its
value in a day. It did not.

**Dividends.** Companies pay out cash to shareholders periodically. On the
payment date the share price mechanically drops by roughly the dividend amount —
the company just gave away cash. Again, the shareholder is not worse off, they
have the cash. But the price series shows a drop with no cause.

**The fix.** Yahoo supplies `adj_close`: the closing price restated so the whole
history is on today's basis, with both effects removed. Comparing `adj_close`
across any two dates gives the return an investor actually experienced.

**But Yahoo only adjusts the close**, leaving open/high/low raw. So the model
derives the factor and applies it to the rest of the bar:

```sql
(adj_close / close) as adj_factor       -- this bar's cumulative adjustment
...
(open * adj_factor) as adj_open,
(high * adj_factor) as adj_high,
(low  * adj_factor) as adj_low,
adj_close,                              -- carried as landed, not recomputed
```

`adj_close` is deliberately **not** recomputed as `close * adj_factor` — that
expression *is* `adj_close` by construction, and would only add a pointless
round-trip through a division. The division is safe because the filter above
guarantees `close > 0`.

### 2.4 Volume needs adjusting too — but dollar volume does not

```sql
(volume / adj_factor) as adj_volume,    -- share count, on the adjusted basis
volume               as raw_volume,     -- as landed
(volume * close)     as dollar_volume,  -- deliberately BOTH raw
```

**Why volume is adjusted.** A 4-for-1 split quadruples the number of shares in
existence, so overnight the daily share count quadruples too. Leaving that raw
puts a false 4× step into every rolling-volume feature. Dividing by
`adj_factor` (which is ~¼ for pre-split rows) multiplies pre-split volume by 4,
putting the whole series on one share basis.

**Why dollar volume is not.** `dollar_volume` is the *money* that changed hands,
and money is already split-invariant: the split divides the price by 4 and
multiplies the share count by 4, so the product is unchanged. Adjusting either
leg would **double-count the correction**.

This is the column the `min_adv_usd` tradability filter is applied against —
"did at least $1M of this stock actually trade?" is a question about money, not
about share counts.

### 2.5 The incremental filter, again on `run_date`

Same reasoning as bronze — see
[§3.1 there](bronze-models-rationale.md#31-filter-on-run_date-not-on-date). A
backfill re-lands old bar dates under a recent `run_date`; filtering on `date`
would leave those corrections permanently outside the window.

`md5_hash` is inherited from bronze and is 1:1 with `(symbol, date)`, which is
what makes it a valid `unique_key` — and there is a
`unique_combination_of_columns` test asserting exactly that, so the assumption
is checked rather than trusted.

---

## 3. `stg_financials_long` — six tables into one fact

The most involved staging model. Four jobs, in order: **union → parse → map →
dedupe**.

### 3.1 What the raw data is

Public companies must file their financial results with the US regulator (the
SEC). Those filings are tagged in **XBRL**, a machine-readable standard where
every number carries a *concept* — a standardised identifier for what the number
means:

```
RevenueFromContractWithCustomerExcludingAssessedTax   85,777,000,000
NetIncomeLoss                                         14,736,000,000
InventoryNet                                           7,286,000,000
```

Think of a concept as a column name from a vocabulary of thousands, where each
filer picks their own subset. The data arrives **long** — one row per number —
which is the only sane way to store a schema that varies per filer.

### 3.2 Job 1: union

Bronze has six mirrors: {income, cash flow, balance sheet} × {quarterly,
annual}. Every consumer wants one long fact table, so the union happens exactly
once, here.

The six are structurally identical except that quarterly tables name their
period column `qtr` and annual ones name it `fy`. A Jinja loop handles it:

```jinja
{% set statement_sources = [
    ('br_income_stmts_quarterly',        'income',        'quarterly', 'qtr'),
    ('br_income_stmts_yearly',           'income',        'annual',    'fy'),
    ...
] %}
```

**The table name becomes two columns** — `statement` and `period_type`. That is
the whole point of the union: a fact you can filter on beats a fact you have to
pick a table for.

### 3.3 Job 2: parse the period label

Period arrives as free text: `'Q1 2024'`, `'FY 2016'`. Useless for date maths,
so it is parsed into a real `fiscal_period_end` date:

```sql
substring(period_label from '([0-9]{4})\s*$')::int  as fiscal_year,
case when period_type = 'quarterly'
     then substring(period_label from '^\s*Q([1-4])')::int
end                                                 as fiscal_quarter
```

then turned into the quarter's last day without hardcoding 30/31:

```sql
(make_date(fiscal_year, fiscal_quarter * 3, 1)
 + interval '1 month' - interval '1 day')::date
```

*First day of the closing month, plus a month, minus a day.*

`fiscal_quarter * 3` is the quarter's **closing month** (Q1→3, Q2→6, Q3→9,
Q4→12), because quarters are three months long and quarter N ends in month 3N.
The `+1 month -1 day` idiom then lands on the last day of that month without the
model needing to know that quarters end 31 / 30 / 30 / 31. It also rolls the
year over on its own: `2024-12-01 + 1 month` is `2025-01-01`, minus a day is
`2024-12-31`.

The `::date` cast is required because `date + interval` returns a `timestamp` in
Postgres, and the column should be a plain date.

**The raw label is kept beside the parsed date.** When a parse is wrong, the
original string is the only way to see what it was wrong about. Cheap column,
high debugging value.

**Two caveats worth knowing:**

- The regexes make the space optional (`FY2016` as well as `FY 2016`) so a
  change in the ingestion label format degrades gracefully instead of silently
  producing NULLs on one shape.
- **This maps to *calendar* quarter ends.** A company whose financial year runs
  July–June has its "Q1 2024" land on 2024-03-31 here, not on its true period
  end. Accurate for the ~75% of the index on a December year-end, off by up to a
  quarter for the rest. EDGAR ingestion does not currently carry the real
  period end; replace this when it does.

Anything matching neither shape yields a NULL and is caught by
`tests/assert_no_unparsed_fiscal_periods.sql`, which **fails the build** rather
than letting an unparsed period become a silent gap. Currently zero.

### 3.4 Job 3: map concepts to canonical names

`RevenueFromContractWithCustomerExcludingAssessedTax` and `Revenues` both mean
revenue. `seeds/concept_map.csv` is the translation table, and it is joined here
— see [`concept-map-rationale.md`](concept-map-rationale.md) for how each
mapping was chosen.

Two deliberate details:

**It is a LEFT join.** An unmapped concept keeps its row with `canonical_name`
NULL, so a count of NULLs *is* the coverage metric. An inner join would make bad
coverage look like clean data.

> **Measured:** 1,878,625 rows, of which **881,693 are mapped (46.9%)**, leaving
> **314 distinct unmapped concepts**. That number is not alarming — the XBRL
> vocabulary has a very long tail of filer-specific concepts nobody needs. It is
> a metric to watch, and it is only visible because of the LEFT join.

**It joins on `(concept, statement)`, not on `concept` alone.** `concept` is
already unique within the seed, so the extra predicate adds no rows. What it
does is **decontaminate cross-statement leakage**: a balance-sheet concept that
turns up inside an income-statement filing does not silently acquire a canonical
name it has no business having.

The join also carries `sign` and `priority` through — the next model needs both,
and re-joining the seed there would be pure waste.

### 3.5 Job 4: dedupe, and what an amendment is

Companies sometimes **restate** results: they file a corrected version of a
report they already filed (a `10-K/A` amending a `10-K`). Both versions are in
the data. You want the corrected one.

The correct rule is *keep the latest `filed_date` per `(cik, metric,
period_end)`*. **EDGAR ingestion does not currently carry `filed_date`**, so
this stands in a proxy — latest ingest wins:

```sql
row_number() over (
    partition by symbol, statement, period_type, fiscal_period_end, concept
    order by execution_id desc, run_date desc, md5_hash desc
) as _row_num
```

**Be honest about what this assumes.** It holds when amendments are ingested
*after* the filings they amend — true for a forward-running feed, **false for an
out-of-order backfill**. The partition is already the right one; when
`filed_date` lands, swap the ordering key and nothing else changes.

`md5_hash` is the final tie-breaker so the survivor is stable across runs rather
than dependent on scan order. Non-deterministic dedup is the kind of bug that
makes two people disagree about the same query.

---

## 4. `stg_companies` — the sector dimension

Cleanup of a Wikipedia scrape of S&P 500 constituents. 503 rows, materialized as
a table (nothing to window on, rebuilds instantly).

Small model, four decisions worth reading:

**Defensive trimming.** `upper(btrim(symbol))` even though the scrape is
currently clean (503 rows, 503 distinct, zero untrimmed). It is a *scrape*. A
stray trailing space becomes a silently empty join, which presents as "this
company has no price data" rather than as an error.

**Do not "fix" the hyphens.** Symbols arrive in Yahoo punctuation — `BRK-B`,
`BF-B`, not `BRK.B`. That is the same form the OHLCV landing tables use, so
joins need no translation. Normalising them to the "correct" dotted form would
break every join in the warehouse. There is a comment saying so; heed it.

**CIK stored twice.** The CIK is EDGAR's company identifier. Kept as `bigint`
for joins, and also as `cik_padded` — the zero-padded 10-character string that
EDGAR's own URLs and JSON payloads require. Deriving the padding once here beats
re-deriving it in every consumer.

**`founded` is free text.** 39 of 503 rows read like `"2013 (1888)"` — the year
the current entity was formed, with its predecessor in parentheses. The model
takes the leading year into `founded_year` and keeps the raw string in
`founded_raw`. Same pattern as the period label: parse into a typed column,
retain the original for when the parse is wrong.

---

## 5. Conventions that hold across the layer

**Uppercase columns are a bronze-and-below concern.** Landing and the ingestion
configs are uppercase (`SYMBOL`, `DATE`, `QTR`); bronze renames to snake_case.
Silver never quotes an identifier.

**Every model is `delete+insert`, never `append`.** Re-running a load must be a
no-op. See [bronze §3.2](bronze-models-rationale.md#32-deleteinsert-not-append).

**Incremental filters key on `run_date`** (ingest time), not `date` (event
time), so backfills are caught.

**No cross-row maths.** Deliberate. Returns and rolling windows start in the
intermediate layer.

**Parse, but keep the raw.** `period_label` beside `fiscal_period_end`,
`founded_raw` beside `founded_year`. Costs a column, saves an afternoon.

---

## 6. Verify

```bash
cd src/transformation/aurum_dwh
uv run --group dbt dbt build --select silver.staging

# how much did the bad-tick filter remove?
uv run --group dbt dbt show --inline "
  select (select count(*) from bronze.br_ohlcv_1d)     as bronze,
         (select count(*) from silver.stg_ohlcv_daily) as staged"

# concept-map coverage
uv run --group dbt dbt show --inline "
  select count(*) total,
         count(canonical_name) mapped,
         round(100.0 * count(canonical_name) / count(*), 1) pct
  from silver.stg_financials_long"
```
