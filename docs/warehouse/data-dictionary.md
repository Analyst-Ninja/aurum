# AURUM — Data Dictionary

> Every field in the warehouse **as built**: Postgres landing (`public`) → `bronze` → `silver` →
> `gold`, all in the local Postgres database `aurum`.
>
> This file documents what exists in code. The Kafka/Snowflake pipeline in
> `docs/architecture/TECHNICAL_SPEC.md` is the target design and is **not** what these tables are.
> For why each model is shaped the way it is, see [`dwh-medallion.md`](dwh-medallion.md).
>
> Sources: Yahoo Finance via `yfinance` (OHLCV) and SEC EDGAR XBRL company facts (statements),
> both through `src/ingestion/`. EDGAR is rate-limited to **10 req/s** and requires an honest
> `User-Agent`. Row counts measured 2026-09-05.

Contents: [Landing](#landing--schema-public) · [Seeds](#seeds) · [Bronze](#bronze) ·
[Silver staging](#silver--staging) · [Silver intermediate](#silver--intermediate) ·
[Gold](#gold) · [Gotchas](#gotchas) · [External endpoints](#key-external-endpoints)

---

## Landing — schema `public`

Written directly by `src/ingestion/` (no Kafka in the code path). Three properties govern everything
downstream:

- **Append-only.** `PostgresDataSource.write_data` uses `to_sql(if_exists='append')`, so re-runs
  duplicate rows. Deduplication is bronze's job.
- **Uppercase, quoted identifiers.** `"SYMBOL"`, `"DATE"`, `"ADJ CLOSE"` — with real spaces in the
  Yahoo columns. Postgres folds unquoted identifiers to lowercase, so the quotes are mandatory.
- **Types are whatever pandas inferred.** Prices are `double precision`; `RUN_DATE` is `text`.

Every landing table carries three columns stamped by `BaseFeed._add_write_metadata`:

| Column | Type | Meaning |
|---|---|---|
| `RUN_DATE` | text | Logical run date passed to the feed (`-d`), `YYYY-MM-DD` |
| `EXECUTION_ID` | text | `YYYYMMDD_HHMMSSssssss` stamp — sorts chronologically as a string, which is what makes latest-write-wins dedup possible |
| `MD5_HASH` | text | Deterministic md5 of the config's `cols_for_pk` values. The dedup key |

### `ohlcv_1d` — daily bars · 2,941,914 rows

Config: `src/ingestion/configs/yahoo/ohlcv_1d.yaml`. `cols_for_pk` = `SYMBOL, DATE`.

| Column | Type | Meaning |
|---|---|---|
| `SYMBOL` | text | Ticker in Yahoo punctuation (`BRK-B`, not `BRK.B`) |
| `DATE` | timestamp | Bar date, midnight |
| `OPEN` / `HIGH` / `LOW` / `CLOSE` | double | Raw (unadjusted) prices |
| `ADJ CLOSE` | double | Split- and dividend-adjusted close. The only adjusted leg Yahoo supplies |
| `VOLUME` | double | Shares traded (a count, delivered as a float) |
| `STOCK SPLITS` | double | Split ratio on the day; `0` on ordinary days |
| `DIVIDENDS` | double | Dividend paid on the day; `0` on ordinary days |

Natural key `(SYMBOL, DATE)`. Coverage: 503 symbols, 2000-01-03 → 2026-09-02.

### `ohlcv_1min` — minute bars · 918,316 rows

Same columns; `DATE` is a `timestamp with time zone` at minute resolution. Config:
`configs/yahoo/ohlcv_1min.yaml`. **Mirrored into bronze but not used by any silver or gold model** —
the warehouse is a daily panel.

### The six EDGAR statement tables

`income_stmts_quarterly` · `income_stmts_yearly` · `cashflow_stmts_quarterly` ·
`cashflow_stmts_yearly` · `balance_sheet_stmts_quarterly` · `balance_sheet_stmts_yearly`.

Configs in `src/ingestion/configs/edgar/`. All six share one shape — **long by XBRL concept**, one row
per reported line item per period. Quarterly tables carry a `QTR` label column, yearly tables carry
`FY`; that is the only structural difference. `cols_for_pk` = `SYMBOL, QTR|FY, CONCEPT`.

| Column | Type | Meaning |
|---|---|---|
| `SYMBOL` | text | Ticker |
| `QTR` *(quarterly)* | text | Period label as landed, e.g. `'Q1 2024'` |
| `FY` *(yearly)* | text | Period label as landed, e.g. `'FY 2016'` |
| `CONCEPT` | text | Raw XBRL concept name, e.g. `NetIncomeLoss`, `RevenueFromContractWithCustomerExcludingAssessedTax` |
| `LABEL` | text | Human-readable label as presented in the filing |
| `VALUE` | double | The number, **in raw dollars** (Apple revenue = `383285000000`), or shares, or a per-share amount, depending on the concept |
| `DEPTH` | double | Indentation depth in the filing's presentation tree |
| `IS_ABSTRACT` | boolean | Presentation-only header row with no value of its own |
| `IS_TOTAL` | boolean | The row is a subtotal/total line |
| `SECTION` | text | Statement section the row was presented under |
| `CONFIDENCE` | double | Extractor confidence in the concept mapping |

Row counts: income 245,320 / 260,772 (quarterly/yearly), cashflow 411,371 / 422,623, balance sheet
278,016 / 260,523.

**What these tables do NOT carry, and what it costs:**

| Missing | Consequence |
|---|---|
| `filed_date` | The point-in-time guard is a **fixed lag approximation**, not a real filing date — see [The lag](#the-point-in-time-lag). Also forces the amendment-supersedes rule to be approximated as *latest ingest wins* |
| `form_type` | Cannot filter to `10-K` / `10-Q` / `8-K`, and cannot tell an amendment from an original |
| `accession_no` | No filing-level identity; rows cannot be grouped by the document they came from |
| `cik` | The EDGAR key is only available via the `company_meta` seed, not the fact rows |
| real `period_end` | The text label is parsed to a **calendar** quarter end, which is wrong for non-December fiscal years |

Adding the first three to `FinancialStmtsDatasource._melt_statement`
(`src/ingestion/datasources/api/edgar/financial_stmts.py`) is tracked in
[GH-47](https://github.com/Analyst-Ninja/aurum/issues/47).

---

## Seeds

Loaded into schema `bronze` by `dbt seed`.

### `company_meta.csv` — 503 rows

The S&P 500 constituent list, scraped from Wikipedia plus `company_tickers.json`. The **only** source
of sector in the warehouse, and therefore required by every `_vs_sector` feature.

| Column | Meaning |
|---|---|
| `symbol` | Ticker, already in Yahoo punctuation — do not "fix" the hyphens |
| `company_name` | Registered name |
| `sector` / `industry` | GICS classification |
| `headquarter_locations` | Free text |
| `date_added` | Date the company entered the index — a survivorship guard |
| `cik` | EDGAR Central Index Key (integer; `stg_companies` also renders the zero-padded 10-char form) |
| `founded` | Free text; 39 rows carry annotations like `"2013 (1888)"` |

### `concept_map.csv` — 84 rows

Maps raw XBRL concepts to canonical names. The single place issuer tag variation is handled.

| Column | Meaning |
|---|---|
| `concept` | Raw XBRL concept name |
| `canonical_name` | AURUM's name, e.g. `revenue`, `net_income`, `operating_cash_flow` |
| `statement` | `income` / `cashflow` / `balance_sheet`. Joined on alongside `concept` so a balance-sheet tag appearing inside an income statement cannot acquire a name it has no business having |
| `sign` | `1`, or `-1` for items EDGAR reports as a positive magnitude but which are cash outflows (capex, buybacks, dividends paid) |
| `priority` | `1` = most trusted. Resolves several concepts competing for one canonical name in a period |

Rationale and measured coverage: `docs/warehouse/rationale/concept-map-rationale.md`.

### `selected_features.csv` — 1 row (placeholder)

Written by the SHAP loop in `src/modeling/`; ships with one placeholder row so
`mart_feature_summary` compiles before anything has been trained.

| Column | Meaning |
|---|---|
| `feature_name` | Column name in `mart_training_set` |
| `rank` | 1 = most important |
| `mean_abs_shap` | Mean absolute SHAP value from the run |
| `selected` | Boolean; only `true` rows are used |
| `model_version` | Which training run produced the row |

Contract detail: `docs/warehouse/rationale/selected-features-seed.md`.

---

## Bronze

Eight `br_*` models, one per landing table. Deduplicate on `md5_hash` (latest `execution_id` wins),
cast types, rename uppercase to snake_case. **No cleaning** — bad ticks and unparsed labels survive
into bronze on purpose.

### `br_ohlcv_1d` / `br_ohlcv_1min`

| Column | Type | From |
|---|---|---|
| `symbol` | text | `"SYMBOL"` |
| `date` *(1d)* / `datetime` *(1min)* | date / timestamp | `"DATE"` |
| `open`, `high`, `low`, `close`, `adj_close` | numeric | `numeric`, not float — drift compounds once returns are built on it |
| `stock_splits`, `dividends` | numeric | |
| `volume` | bigint | it is a share count |
| `run_date` | date | `"RUN_DATE"::date` |
| `execution_id`, `md5_hash` | text | carried for lineage and dedup |

### `br_{income,cashflow,balance_sheet}_stmts_{quarterly,yearly}`

| Column | Type | Note |
|---|---|---|
| `symbol` | text | |
| `qtr` *(quarterly)* / `fy` *(yearly)* | text | period label, still unparsed |
| `concept`, `label`, `section` | text | |
| `depth` | bigint | |
| `is_abstract`, `is_total` | boolean | |
| `confidence`, `value` | numeric | |
| `run_date`, `execution_id`, `md5_hash` | date/text | |

---

## Silver — staging

### `stg_ohlcv_daily` — grain `(symbol, date)` · 2,935,412 rows

Bad-tick filter (6,502 rows dropped), split/dividend adjustment, and bar-local derivations only.

| Column | Type | Definition |
|---|---|---|
| `symbol`, `date` | text, date | Grain |
| `adj_factor` | numeric | `adj_close / close` — that bar's cumulative split+dividend factor |
| `adj_open`, `adj_high`, `adj_low` | numeric | raw leg × `adj_factor` |
| `adj_close` | numeric | carried as landed (it *is* `close × adj_factor` by construction) |
| `adj_volume` | numeric | `volume / adj_factor` — a 4:1 split otherwise puts a false step into every rolling-volume feature |
| `raw_volume` | bigint | as landed |
| `dollar_volume` | numeric | **raw** `close × volume`. Notional traded is already split-invariant, so adjusting either leg double-counts |
| `stock_splits`, `dividends` | numeric | |
| `run_date`, `execution_id`, `md5_hash` | | lineage; `md5_hash` is 1:1 with `(symbol, date)` and is the model's `unique_key` |

Dropped: null or zero volume, non-positive `open`/`high`/`low`/`close`/`adj_close`, and bars whose
high/low do not bound their own open and close.

### `stg_financials_long` — grain `(symbol, statement, period_type, fiscal_period_end, concept)` · 1,878,625 rows

The six statement mirrors unioned, tagged, date-parsed, concept-mapped and deduped. **Still long.**

| Column | Type | Definition |
|---|---|---|
| `symbol` | text | |
| `statement` | text | `income` / `cashflow` / `balance_sheet` — was a table name, now a column |
| `period_type` | text | `quarterly` / `annual` |
| `period_label` | text | raw label as landed (`'Q1 2024'`), kept verbatim because the parse is the fragile step |
| `fiscal_year`, `fiscal_quarter` | integer | parsed out of the label |
| `fiscal_period_end` | date | last day of the **calendar** quarter/year — see gotchas |
| `concept` | text | raw XBRL concept |
| `canonical_name` | text | from `concept_map`; **NULL when unmapped**, deliberately, so coverage stays measurable (53% of rows are unmapped today) |
| `canonical_sign` | integer | `1` / `-1`, carried for the pivot downstream |
| `concept_priority` | integer | 1 = most trusted, for collision resolution downstream |
| `value` | numeric | as reported, raw dollars |
| `label`, `depth`, `is_abstract`, `is_total`, `section`, `confidence` | | XBRL presentation context |
| `run_date`, `execution_id`, `md5_hash` | | lineage |

Dedup keeps the highest `execution_id` per grain — the *latest ingest wins* stand-in for the spec's
amendment-supersedes rule.

### `stg_companies` — grain `(symbol)` · 503 rows

| Column | Type | Definition |
|---|---|---|
| `symbol` | text | trimmed and uppercased |
| `company_name`, `sector`, `industry`, `headquarter_location` | text | trimmed |
| `date_added` | date | index entry date |
| `cik` | bigint | numeric EDGAR key |
| `cik_padded` | text | zero-padded 10-char form used by EDGAR URLs and JSON |
| `founded_year` | integer | leading 4-digit year parsed from `founded` |
| `founded_raw` | text | the original free text beside it |

---

## Silver — intermediate

### `int_market_daily` — grain `(date)` · 6,707 rows

Equal-weight universe aggregate. Both the beta denominator and the regime feature block.

| Column | Definition |
|---|---|
| `market_log_ret` | equal-weight mean of member daily log returns |
| `market_ret_1d` | `exp(market_log_ret) - 1` |
| `market_ret_21d`, `market_ret_63d` | `exp(sum(market_log_ret) over N) - 1` |
| `market_vol_21d`, `market_vol_63d` | `stddev_samp(market_log_ret) over N × sqrt(252)` |
| `market_breadth` | share of the universe above its own 50-day MA (symbols with an incomplete 50-bar window excluded) |
| `market_xs_dispersion` | cross-sectional stddev of member returns that day. High = stock selection pays; low = a beta day |
| `universe_size`, `universe_with_return` | member counts, the second excluding symbols with no return that day |

### `int_technicals_daily` — grain `(symbol, date)` · 2,935,412 rows

Every price-derived feature; fundamental-free, so no point-in-time guard is needed. Columns:
`ret_1d`, `ret_5d`, `ret_21d`, `ret_63d`, `ret_126d`, `ret_252d`, `mom_12_1`, `reversal_5d`,
`gap_overnight`, `ma_10`, `ma_20`, `ma_50`, `ma_200`, `price_to_ma_50`, `price_to_ma_200`,
`ma_50_over_200`, `high_252d`, `low_252d`, `dist_from_52w_high`, `dist_from_52w_low`, `vol_21d`,
`vol_63d`, `vol_252d`, `parkinson_vol_21d`, `downside_dev_21d`, `atr_14`, `atr_pct`,
`max_drawdown_252d`, `sharpe_21d`, `sharpe_63d`, `beta_252d`, `idio_vol_252d`, `market_corr_252d`,
`adv_21d`, `volume_zscore_21d`, `amihud_illiq_21d`, `obv_flow_21d`, `rsi_14`, `macd`, `macd_signal`,
`macd_hist`, `bollinger_pctb_20`, `bollinger_width_20`, `stoch_k_14`, plus `adj_close`, `adj_volume`
and `dollar_volume` carried through.

**Formulas for all of these are in
[`dwh-medallion.md` § Feature catalogue](dwh-medallion.md#feature-catalogue).**

### `int_fundamentals_wide` — grain `(symbol, period_type, fiscal_period_end)` · 20,032 rows

Concept collisions resolved by seed priority, `canonical_name` pivoted to columns, TTM added.

**Pivoted line items** (raw, as reported, sign-normalized): `revenue`, `cost_of_revenue`,
`operating_income`, `operating_expenses`, `net_income`, `pretax_income`, `tax_expense`,
`interest_expense`, `eps_basic`, `eps_diluted`, `shares_basic`, `shares_diluted`, `total_assets`,
`total_liabilities`, `total_equity`, `current_assets`, `current_liabilities`, `cash_and_equivalents`,
`short_term_investments`, `inventory`, `receivables`, `long_term_debt`, `long_term_debt_current`,
`ppe_net`, `goodwill`, `retained_earnings`, `operating_cash_flow`, `investing_cash_flow`,
`financing_cash_flow`, `capex`, `depreciation_amortization`, `share_based_comp`, `dividends_paid`,
`buybacks`, `dividends_per_share`, `minority_interest`, `gross_profit_reported`.

**Derived:**

| Column | Definition |
|---|---|
| `gross_profit` | `coalesce(gross_profit_reported, revenue - cost_of_revenue)` — a filed subtotal beats a reconstruction |
| `eps` | `coalesce(eps_diluted, eps_basic)` — diluted is the conservative count valuation uses |
| `shares_outstanding` | `coalesce(shares_diluted, shares_basic)` |
| `total_debt` | `long_term_debt + long_term_debt_current` — interest-bearing only; payables and leases excluded |
| `ebitda` | `operating_income + depreciation_amortization`; NULL if either leg is missing |
| `free_cash_flow` | `operating_cash_flow + capex` (capex is already negative) |

**TTM columns** — *trailing twelve months*, the last four quarters summed to remove seasonality:
`revenue_ttm`, `gross_profit_ttm`, `operating_income_ttm`, `net_income_ttm`, `pretax_income_ttm`,
`tax_expense_ttm`, `interest_expense_ttm`, `operating_cash_flow_ttm`, `capex_ttm`,
`depreciation_amortization_ttm`, `share_based_comp_ttm`, `ebitda_ttm`, `eps_ttm`. Annual rows
short-circuit (already twelve months); a window missing any of its four quarters, or spanning outside
240–320 days, yields NULL rather than a mislabelled partial sum. Balance-sheet stocks get no TTM —
a level is already point-in-time.

| Column | Definition |
|---|---|
| `available_from` | **The point-in-time guard.** `fiscal_period_end + 60 days` (quarterly) or `+ 90 days` (annual). Every downstream join keys on this and never on `fiscal_period_end` |

### `int_fundamental_ratios` — same grain · 20,032 rows

Carries all of `int_fundamentals_wide` through, and adds price-free ratios:

| Family | Columns |
|---|---|
| Growth | `{revenue,eps,net_income,ocf,shares_change}_growth_qoq` and `_growth_yoy` |
| Profitability | `gross_margin`, `operating_margin`, `net_margin`, `roe`, `roa`, `roic` |
| Leverage | `debt_to_equity`, `current_ratio`, `interest_coverage`, `net_debt_to_ebitda` |
| Quality | `accruals`, `ocf_to_net_income`, `asset_turnover`, `capex_to_revenue` |
| Derived flow | `free_cash_flow_ttm` |

Formulas: [`dwh-medallion.md` § Fundamental ratios](dwh-medallion.md#fundamental-ratios--int_fundamental_ratios).
Valuation ratios are **not** here — they need a price.

### `int_features_daily` — grain `(symbol, date)` · 2,935,412 rows

The as-of join: every bar gets the newest fundamental row whose `available_from` is on or before that
date, via a half-open validity interval. Carries every `int_fundamental_ratios` column plus:

| Column | Definition |
|---|---|
| `close_raw` | `adj_close / adj_factor` — the **unadjusted** price, which is what a share count and EPS from the filing must be priced against |
| `market_cap` | `close_raw × shares_outstanding` |
| `price_to_earnings` | `close_raw / eps_ttm` |
| `earnings_yield` | `eps_ttm / close_raw` — the well-behaved inverse, and what cross-sectional ranking uses |
| `price_to_sales` | `market_cap / revenue_ttm` |
| `price_to_book` | `market_cap / total_equity` |
| `fcf_yield` | `free_cash_flow_ttm / market_cap` |
| `enterprise_value` | `market_cap + total_debt − cash − short_term_investments` |
| `ev_to_ebitda` | `enterprise_value / ebitda_ttm` |
| `debt_to_market_cap` | `total_debt / market_cap` |
| `turnover_21d` | `avg_volume_21d / shares_outstanding` |
| `days_since_available` | `date − available_from`. Never negative; a run past ~130 means a filing was missed |
| `avg_volume_21d`, `adj_factor`, `adj_volume`, `dollar_volume` | carried through |

Valuation ratios move **every trading day** even though the fundamental leg changes four times a
year. That is intended.

---

## Gold

### `mart_features` — grain `(symbol, date)` · 2,935,412 rows

**The feature store, and what live inference reads.** Contains **no target columns at all**, by
construction. Blocks:

| Block | Columns |
|---|---|
| Identity | `symbol`, `date`, `sector`, `industry` |
| Price | `adj_close`, `close_raw`, `adj_volume`, `dollar_volume` |
| Technicals | all 44 feature columns from `int_technicals_daily`, plus `turnover_21d` |
| PIT provenance | `period_type`, `fiscal_period_end`, `fundamental_available_from`, `days_since_available` |
| Fundamentals (raw) | `revenue`, `net_income`, `eps_basic`, `eps_diluted`, `eps_ttm`, `revenue_ttm`, `net_income_ttm`, `operating_cash_flow_ttm`, `free_cash_flow_ttm`, `ebitda_ttm`, `total_assets`, `total_equity`, `total_debt`, `long_term_debt`, `cash_and_equivalents`, `shares_outstanding` |
| Valuation | `market_cap`, `price_to_earnings`, `earnings_yield`, `price_to_sales`, `price_to_book`, `fcf_yield`, `enterprise_value`, `ev_to_ebitda`, `debt_to_market_cap` |
| Profitability | `gross_margin`, `operating_margin`, `net_margin`, `roe`, `roa`, `roic` |
| Growth | `revenue_growth_qoq/_yoy`, `eps_growth_qoq/_yoy`, `net_income_growth_yoy`, `ocf_growth_yoy`, `shares_change_growth_yoy` |
| Leverage & quality | `debt_to_equity`, `current_ratio`, `interest_coverage`, `net_debt_to_ebitda`, `accruals`, `ocf_to_net_income`, `asset_turnover`, `capex_to_revenue` |
| Regime | `market_ret_1d/_21d/_63d`, `market_vol_21d/_63d`, `market_breadth`, `market_xs_dispersion` |
| Calendar | `day_of_week`, `month_of_year`, `is_month_end`, `is_quarter_end` (trading calendar, not civil) |
| Cross-sectional | `<feature>_z`, `<feature>_decile`, `<feature>_vs_sector` for each of 36 curated features |

**The cross-sectional suffixes:**

| Suffix | Definition |
|---|---|
| `_z` | winsorized at the 1st/99th percentile **of that date**, then z-scored across the universe |
| `_decile` | rank within that date, 1 = lowest, 10 = highest; nulls excluded from the buckets |
| `_vs_sector` | raw value minus that date's GICS sector median |

The 36 features carrying all three: `ret_21d`, `ret_63d`, `ret_126d`, `mom_12_1`, `reversal_5d`,
`price_to_ma_50`, `dist_from_52w_high`, `vol_21d`, `vol_252d`, `max_drawdown_252d`, `sharpe_63d`,
`beta_252d`, `idio_vol_252d`, `adv_21d`, `amihud_illiq_21d`, `turnover_21d`, `rsi_14`, `macd_hist`,
`bollinger_pctb_20`, `market_cap`, `earnings_yield`, `price_to_sales`, `price_to_book`, `fcf_yield`,
`ev_to_ebitda`, `gross_margin`, `net_margin`, `roe`, `roic`, `revenue_growth_yoy`, `eps_growth_yoy`,
`shares_change_growth_yoy`, `debt_to_equity`, `net_debt_to_ebitda`, `accruals`, `asset_turnover`.

### `mart_training_set` — grain `(symbol, date)` · 2,895,171 rows

`mart_features` plus targets and fold, filtered to tradable names (`close_raw >= 1.0`,
`adv_21d >= 1,000,000`). **The only model in the warehouse that looks into the future.**

| Column | Definition |
|---|---|
| `fwd_ret_5d`, `fwd_ret_21d` | `ln(lead(adj_close, N) / adj_close)` — forward log returns |
| `fwd_ret_5d_excess` | `fwd_ret_5d` minus the tradable-universe mean that date |
| `fwd_ret_5d_xs_decile` | cross-sectional decile of `fwd_ret_5d` within the date — the ranking label |
| `label_up_5d` | `1` / `0` / **NULL** when the return is null |
| `fold_id` | `dense_rank()` over calendar month — walk-forward, never random. 321 folds today |

All four targets are **NULL for the last N trading dates by construction** and must stay null;
`tests/assert_targets_null_at_edge.sql` enforces it.

### `mart_feature_summary` — grain `(symbol, date)` · 2,895,171 rows

`mart_training_set` narrowed to the features named in `seeds/selected_features.csv`. Always carries
the keys and targets regardless of the seed: `symbol`, `date`, `sector`, `fold_id`, `fwd_ret_5d`,
`fwd_ret_21d`, `fwd_ret_5d_excess`, `fwd_ret_5d_xs_decile`, `label_up_5d`. With the placeholder seed
it falls back to the full training set — an unselected panel, never an empty one.

### `mart_stock_screener` — grain `(symbol)` · 503 rows

Latest bar **per symbol** (a delisted or renamed name keeps its last known row rather than
vanishing). The query target for the future FastMCP server: flat headline numbers only, no window
functions to explain.

| Group | Columns |
|---|---|
| Identity | `symbol`, `company_name`, `sector`, `industry` |
| Market state | `price_date`, `latest_price` (= `close_raw`), `adj_close`, `adv_21d` |
| Filing provenance | `period_end`, `period_type`, `fundamental_available_from`, `days_since_available` |
| Fundamentals | `revenue`, `net_income`, `eps_basic`, `eps_ttm`, `long_term_debt`, `total_debt`, `shares_outstanding` |
| Valuation | `market_cap`, `enterprise_value`, `price_to_earnings`, `price_to_sales`, `price_to_book`, `ev_to_ebitda`, `fcf_yield`, `debt_to_market_cap` |
| Profitability & growth | `gross_margin`, `operating_margin`, `net_margin`, `roe`, `roic`, `revenue_growth_qoq`, `revenue_growth_yoy`, `eps_growth_yoy` |
| Balance-sheet health | `debt_to_equity`, `current_ratio`, `net_debt_to_ebitda` |
| Trend & risk | `ret_21d`, `ret_252d`, `ma_50`, `ma_200`, `price_to_ma_50`, `price_to_ma_200`, `dist_from_52w_high`, `vol_21d`, `vol_252d`, `sharpe_21d`, `beta_252d`, `max_drawdown_252d`, `rsi_14` |
| Cross-sectional anchors | `earnings_yield_decile`, `net_margin_vs_sector` |

Two deliberate absences: there is **no** `ma_30d`/`ma_90d`/`vol_30d`/`sharpe_30d` (silver builds
10/20/50/200 and 21/63/252, the conventional trading-day windows), and **no** `sentiment_7d` /
`news_count_7d` — news is not ingested anywhere, and a column of nulls would suggest otherwise.

---

## The point-in-time lag

Stated plainly, because it is the warehouse's most important approximation:

> **AURUM does not know when a filing was actually filed.** EDGAR ingestion carries no `filed_date`,
> so `available_from` is `fiscal_period_end` plus a fixed lag — 60 days for quarterly filings, 90 for
> annual — rather than the real filing date.

Filings land ~43 days after period end on average and up to ~61. The lags sit deliberately past that:
under-lagging invents alpha that never existed; over-lagging only costs freshness. Measured mean
staleness (`days_since_available`) across `mart_features` is **126 days**.

The same gap forces the amendment rule to be approximated: `stg_financials_long` keeps the row with
the highest `execution_id` per grain (*latest ingest wins*) instead of the latest `filed_date`. That
is correct for a forward-running feed and wrong for an out-of-order backfill.

Two tests guard the shape of the guard — `tests/assert_no_fundamental_lookahead.sql` inside silver and
`tests/assert_no_lookahead.sql` at the gold boundary — but neither can detect that the lag itself is a
guess. Full discussion:
[`dwh-medallion.md` § The point-in-time lag decision](dwh-medallion.md#the-point-in-time-lag-decision-and-what-it-costs).

---

## Gotchas

| Gotcha | What to do |
|---|---|
| Landing appends; re-runs duplicate rows | Never read `public.*` outside bronze. Bronze dedups on `md5_hash`, latest `execution_id` |
| Landing identifiers are uppercase **and contain spaces** (`"ADJ CLOSE"`) | Quote them. Unquoted `DATE` folds to `date` and does not exist |
| No `filed_date` | The PIT guard is a lag approximation; the amendment rule is *latest ingest wins* |
| `fiscal_period_end` is a **calendar** quarter end | Correct for the ~75% of the index on a December fiscal year; off by up to a quarter otherwise. It also produces future-dated period ends (max in the warehouse: 2027-06-30) |
| One canonical name, several competing concepts | `concept_map.priority` resolves it; `Revenues` and `RevenueFromContractWithCustomerExcludingAssessedTax` both map to `revenue` |
| 53% of `stg_financials_long` rows have a NULL `canonical_name` | Expected — unmapped concepts are kept so coverage stays measurable. Coverage by *value* is what matters; see `docs/warehouse/rationale/concept-map-rationale.md` |
| `VALUE` is raw dollars | Do not rescale on ingest; format at presentation |
| `capex`, `buybacks`, `dividends_paid` are **negative** after `canonical_sign` | `free_cash_flow = ocf + capex` is an addition. Getting it backwards doubles the capex charge |
| Adjusted vs raw price | Every technical uses `adj_close`. Every valuation ratio uses `close_raw = adj_close / adj_factor`, because share counts and EPS are as-reported. Mixing them understates historical market caps |
| `dollar_volume` uses raw price × raw volume | Notional traded is split-invariant already; adjusting either leg double-counts |
| Rolling windows are **ROWS**-based | "252 days" means 252 bars, not 252 calendar days |
| Incremental models read 900 days and write 90 | Violating that produces silently NULL long-window features, not an error. See [the rule](dwh-medallion.md#the-incremental-lookback-rule) |
| Targets are NULL at the right edge | Never `coalesce` them to zero — a test enforces this |
| `mart_features` must stay target-free | Live inference reads it; a target column there is a target column in production |
| SEC 10 req/s limit and mandatory `User-Agent` | Get it from `src.utils.env.get_sec_user_agent()`; do not hardcode |

---

## Key External Endpoints

| Endpoint | Purpose |
|---|---|
| `https://www.sec.gov/files/company_tickers.json` | ticker → CIK for all public companies |
| `https://data.sec.gov/submissions/CIK{cik}.json` | full filing history for one company |
| `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json` | all facts ever reported — the main EDGAR source |
| `https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{concept}.json` | one metric, one company (lighter) |
| `https://www.sec.gov/Archives/edgar/daily-index/{Y}/QTR{N}/master.{YYYYMMDD}.idx` | daily new-filings index — the incremental trigger (`docs/ingestion/edgar-incremental-ingestion.md`) |
| Wikipedia *List of S&P 500 companies* | ticker universe, sector and CIK — the `company_meta` seed |
