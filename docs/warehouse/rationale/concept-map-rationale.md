# concept_map — rationale and evidence

> Companion to `src/transformation/aurum_dwh/seeds/concept_map.csv`.
> Built 2026-09-03 against the landing tables as they stood: 501 symbols,
> 328 distinct XBRL concepts (154 income / 62 balance sheet / 185 cash flow,
> overlapping across statements), 3 ingestion runs.
> Every number in this document was measured, not estimated.

---

## 1. What this seed does and why it exists

`stg_financials_long` cannot pivot EDGAR facts into columns without a mapping from raw XBRL concept strings to canonical field names. Different companies tag the same economic quantity differently, and the tag they choose depends on which accounting standard they adopted and when.

Of the 328 concepts present, **84 are mapped** to 66 canonical fields (32 income, 29 balance sheet, 23 cash flow); 8 carry `sign = -1`. The remaining ~250 are a long tail that feeds no planned feature.

The cost of getting this wrong, measured on revenue:

| Mapping for `revenue` | `(symbol, quarter)` coverage |
|---|---|
| `Revenues` only | **26.8%** |
| `+ RevenueFromContractWithCustomerExcludingAssessedTax` | 90.6% |
| `+ IncludingAssessedTax`, `+ NotFromContract` | **95.0%** |

A hand-written map from the obvious tag name would have discarded three quarters of the revenue data. Everything in this seed is derived from the concepts actually present in the landing tables, cross-checked against published accounting guidance.

### Schema

```csv
concept,canonical_name,statement,sign,priority
```

| Column | Meaning |
|---|---|
| `concept` | Raw XBRL concept string, exactly as landed |
| `canonical_name` | Target column in `int_fundamentals_wide` |
| `statement` | `income` \| `balance_sheet` \| `cashflow`. **A filter, and part of the key** — see §3.1 |
| `sign` | `1`, or `-1` where a value is reported as a positive magnitude but represents an outflow |
| `priority` | Tiebreak when several concepts compete for one `canonical_name` in the same `(symbol, period)`. Silver takes the lowest priority holding a non-null value. |

**`sign` convention:** multiply the reported value to obtain cash-flow polarity — inflows positive, outflows negative. `capex` therefore becomes negative and `fcf = ocf + capex`. Ratios that want a magnitude (`capex_to_revenue`) apply `abs()`.

### How silver consumes it

```sql
select distinct on (symbol, period, canonical_name)
       symbol, period, canonical_name, value * sign as value
from   facts f
join   {{ ref('concept_map') }} m
  on   f.concept = m.concept and f.statement = m.statement
where  f.is_abstract = false and f.value is not null
order  by symbol, period, canonical_name, m.priority
```

Verified: this produces 128,799 rows across 128,799 distinct keys — exactly one value per key.

### Current shape

**84 mappings across 66 canonical fields.** Every mapped concept was confirmed present in the landing tables; every canonical field has non-zero coverage in both the quarterly and yearly tables.

---

## 2. The measurement method

**Always count distinct `(SYMBOL, QTR)` or distinct `SYMBOL`. Never `count(*)`.**

The landing tables append on every ingestion run and currently hold 3 executions. Raw row counts are inflated by roughly 3× and will drift every time a feed runs. Example — `ABBV Q1 2023`, concept `RevenueFromContractWithCustomerExcludingAssessedTax`:

```
n=3   distinct EXECUTION_ID=3   distinct VALUE=1
```

Three rows, one fact. Coverage measured on `count(*)` is meaningless.

**Always filter `IS_ABSTRACT = false`.** 17,480 income rows are section headings (`"Operating expenses:"`) carrying no value.

---

## 3. Decision rules

Derived by working `revenue` and `long_term_debt` end to end. Apply in order.

### 3.1 The key is `(concept, statement)`, not `concept`

edgartools contaminates each statement with facts belonging to the others:

| Concept | On income | On cashflow |
|---|---|---|
| `NetIncomeLoss` | 477 symbols | 477 symbols |
| `IncomeTaxExpenseBenefit` | 495 | 495 |
| `Goodwill` | — | 435 (a balance-sheet fact) |
| `AmortizationOfIntangibleAssets` | 316 | 316 |

`stg_financials_long` unions all six bronze tables. Keying on `concept` alone would map `NetIncomeLoss` to `net_income` twice, producing duplicate rows per `(symbol, quarter, canonical_name)` and breaking the Phase 3 pivot.

Each concept is mapped from exactly **one** statement — its authoritative home. Verified: no `canonical_name` in this seed is sourced from more than one statement.

### 3.2 Check `IS_TOTAL`, but decide per field

`IS_TOTAL` marks a row as a subtotal of the rows beneath it. It was decisive for revenue and useless for debt.

For **revenue**, it eliminated four candidates immediately:

| Concept | IS_TOTAL | Verdict |
|---|---|---|
| RevenueFromContractWithCustomerExcludingAssessedTax | true | keep |
| Revenues | true | keep |
| CostOfRevenue | false | reject — a cost |
| InsuranceServicesRevenue | false | reject — segment component |
| RevenueNotFromContractWithCustomerOther | false | reject — partial component |

For **debt**, *nothing* is `IS_TOTAL = true`. Debt is a leaf line item, not a subtotal, so the filter would have eliminated every candidate.

> Rule: require `IS_TOTAL = true` when the canonical field *is* a subtotal (revenue, gross profit, total assets, operating income). Ignore it when the field is a genuine line item (R&D expense, inventory, capex, debt).

### 3.3 Read the symbol list before trusting a name

`InterestRevenueExpenseNet` passes the `IS_TOTAL` test and matches a `%revenue%` search. Its users:

```
EME, EXC, FISV, IBM, LOW, MAR, NUE, PFE, PNC, RF, ROK, TJX
```

Retailers, pharma, industrials — not banks. It is an incidental net-interest line mis-tagged as a total. Mapping it to `revenue` would have set Lowe's revenue to near zero. Observed directly:

```
EME Q1 2022: InterestRevenueExpenseNet = 0.00B  |  Revenues = 2.59B
```

Flags cannot catch this. Reading the symbol list can.

### 3.4 Verify arithmetic before assuming hierarchy

`LongTermDebt` sounds like the total of current plus noncurrent. It is not:

```
SYM  QTR       current  noncurr  LTDebt   cur+noncur  matches?
ALB  Q1 2020      0.04     3.11    3.11         3.14   False
ALB  Q1 2022      0.50     1.99    1.99         2.49   False
ALB  Q1 2025      0.41     3.13    3.13         3.54   False
```

`LongTermDebt` equals `LongTermDebtNoncurrent` **exactly** in every sampled row. Despite the name it is a synonym for the noncurrent portion. One query reversed the obvious reading.

### 3.5 Separate alternative tags from components

Two different problems that look alike:

| Situation | Resolution |
|---|---|
| `Revenues` vs `RevenueFromContract…` — same quantity, different tag | **`priority`** — pick one |
| `LongTermDebtCurrent` + `LongTermDebtNoncurrent` — different quantities | **separate canonical fields**, summed in `int_fundamental_ratios` |

Collapsing components into one canonical name double-counts. Splitting alternatives into separate fields creates spurious nulls.

### 3.6 Measure the coverage ceiling and stop

Adding tags has diminishing and sometimes zero returns:

```
302 / 501 symbols   LongTermDebtNoncurrent
359 / 501 symbols   + LongTermDebt
359 / 501 symbols   + LongTermDebtAndCapitalLeaseObligations   <- adds nothing
```

The third tag is fully redundant. It is retained (harmless, priority 3) but the search stopped there. `capex` has no alternative tags at all — `PaymentsToAcquireProductiveAssets`, `PaymentsForCapitalImprovements` and `PaymentsToAcquireOtherPropertyPlantAndEquipment` all have **0** symbols in this dataset.

---

## 4. Coverage achieved

Distinct symbols out of 501, after applying the full mapping.

### Strong (≥ 400 symbols quarterly)

| Field | QTR | FY | Note |
|---|---|---|---|
| `investing_cash_flow` | 501 | 501 | |
| `financing_cash_flow` | 501 | 501 | |
| `operating_cash_flow` | 500 | 499 | |
| `total_assets` | 496 | 501 | |
| `total_liabilities_and_equity` | 496 | 501 | |
| `tax_expense` | 495 | 499 | |
| `revenue` | **491** | 465 | 4 tags; `Revenues` alone = 140 |
| `eps_diluted` | 491 | 493 | |
| `eps_basic` | 489 | 492 | |
| `shares_basic` | 488 | 492 | |
| `shares_diluted` | 488 | 494 | |
| `depreciation_amortization` | **483** | 492 | 4 tags; `Depreciation` alone adds 53 symbols |
| `net_income` | 477 | 492 | |
| `total_equity` | 477 | 489 | |
| `retained_earnings` | 476 | 490 | |
| `cash_and_equivalents` | 453 | 481 | |
| `buybacks` | 445 | 471 | |
| `ppe_net` | 442 | 469 | |
| `goodwill` | 440 | 466 | |
| `share_based_comp` | 440 | 487 | 2 tags |
| `pretax_income` | 436 | 449 | |
| `additional_paid_in_capital` | 431 | 452 | 2 tags |
| `current_assets` | 416 | 423 | |
| `current_liabilities` | 416 | 423 | |
| `interest_expense` | **411** | 454 | 3 tags; `InterestExpense` alone = 349 |
| `sga_expense` | 401 | 404 | 2 tags |

### Adequate (250–400)

`operating_income` 398 · `dividends_paid` 395 · `operating_lease_asset` 390 · `common_stock_value` 390 · `long_term_debt` 359 · `total_liabilities` 357 · `capex` 357 · `receivables` 356 · `operating_lease_liability` 346 · `acquisitions` 343 · `deferred_tax_expense` 339 · `accounts_payable` 331 · `cost_of_revenue` 328 · `dividends_per_share` 324 · `amortization_intangibles` 316 · `operating_lease_liability_current` 316 · `deferred_tax_liabilities` 311 · `intangible_assets` 306 · `minority_interest` 296 · `gross_profit` 293 · `inventory` 291 · `debt_issued` 278 · `debt_repaid` 276 · `net_income_to_minority` 274 · `interest_paid` 269 · `change_in_inventory` 263 · `taxes_paid` 251

### Weak (< 250) — use with care

| Field | QTR | Why it is low |
|---|---|---|
| `long_term_debt_current` | 241 | Only companies with debt maturing within 12 months |
| `change_in_receivables` | 237 | Working-capital detail is optional in condensed statements |
| `net_income_to_common` | 210 | Only reported when preferred dividends exist |
| `change_in_payables` | 200 | as above |
| `accrued_liabilities` | 192 | Often folded into "other current liabilities" |
| `stock_issued` | 160 | |
| `rnd_expense` | 149 | Only R&D-intensive firms report it separately |
| `operating_expenses` | 120 | Presentation varies widely |
| `short_term_investments` | 116 | |
| `interest_income` | 112 | |
| `nonoperating_income` | 94 | |
| `total_costs_and_expenses` | 91 | |
| `selling_marketing_expense` | 63 | Usually folded into SG&A |

**Do not build a core feature on a field below ~250 symbols.** Half the universe would be null, and the model would learn "reports this tag" as a proxy for industry.

---

## 5. Per-field evidence for contested decisions

Fields with a single obvious tag are omitted. These are the ones that required a judgement.

### `revenue`

| Concept | Symbols | Priority | Reasoning |
|---|---|---|---|
| RevenueFromContractWithCustomerExcludingAssessedTax | 331 | 1 | ASC 606 element, widest coverage |
| Revenues | 140 | 2 | Pre-606 general element |
| TotalRevenue_Consolidated | 6 | 3 | Synthetic (underscore = edgartools-generated, not `us-gaap`) |
| RevenueFromContractWithCustomerIncludingAssessedTax | 47 | 4 | `IS_TOTAL = false`; includes sales tax |

FASB Data Quality Committee rule [DQC_0067](https://github.com/DataQualityCommittee/dqc_us_rules/blob/master/docs/DQC_US_0067/DQC_0067.md) states that legacy revenue elements and ASC 606 elements "should not be combined in the same filing" — filers commit to one regime. That is precisely why priority (pick one) rather than summation (add them) is the correct resolution.

**On including the `IncludingAssessedTax` variant at priority 4**, despite `IS_TOTAL = false`: of the 47 symbols using it, **33 also report a proper total** and resolve via priority. Only **14** depend on it. Including sales tax slightly overstates revenue for those 14; the alternative is 14 companies with no revenue at all. Drop this row if you prefer strictness.

`SalesRevenueNet` — the deprecated pre-606 element — has 1 symbol and is excluded.

### `long_term_debt`

| Concept | Symbols | Priority |
|---|---|---|
| LongTermDebtNoncurrent | 302 | 1 |
| LongTermDebt | 107 | 2 |
| LongTermDebtAndCapitalLeaseObligations | 5 | 3 |

Ceiling: **359 / 501 = 72%.** Revenue reaches 98%. The gap is genuine — debt-free companies plus banks and insurers using entirely different tags.

**Consequence: null ≠ zero.** A missing `long_term_debt` may mean debt-free (Nvidia-like) or wrong-tag (financial sector). `coalesce(long_term_debt, 0)` would tell the model every bank is debt-free. Leave it null and let the gradient-boosted model handle missingness natively.

**No short-term borrowing tags exist.** Zero symbols report `ShortTermBorrowings`, `CommercialPaper`, `DebtCurrent`, `NotesPayableCurrent`, or `LinesOfCreditCurrent`. So:

```
total_debt = long_term_debt + long_term_debt_current
```

and nothing else. Companies funded by commercial paper will understate.

### Operating leases — mapped, but deliberately not debt

346 symbols report `OperatingLeaseLiabilityNoncurrent` — **better coverage than debt itself**. They are given their own canonical fields and are **not** folded into `long_term_debt`.

Reasoning: [FASB explicitly characterizes operating leases as operating liabilities and not debt](https://www.usbank.com/corporate-and-commercial-banking/insights/credit-finance/equipment/leveraging-asc-842-accounting-leases.html). `docs/warehouse/data-dictionary.md` defines `debt_to_market_cap` on long-term debt alone, so excluding leases is the internally consistent choice. Rating agencies do adjust for leases, and post-ASC-842 research finds [credit ratings decline ~1.5% on average after adoption](https://ileasepro.com/blog/asc842-impacts-on-financial-ratios-and-covenants/) — so a lease-adjusted leverage variant is a legitimate future feature. Keeping the components separate makes that additive rather than a redefinition.

### `total_equity` — parent-attributable

`StockholdersEquity` (477 symbols) is equity attributable to the parent; per [XBRL US guidance](https://xbrl.us/guidance/specific-non-controlling-interest-elements/) the `…IncludingPortionAttributableToNoncontrollingInterest` variant is the consolidated figure. Parent-attributable is the correct ROE and book-value denominator, since `net_income` is likewise parent-attributable — mixing them would compute a ratio across inconsistent scopes.

The NCI-inclusive variant has **0 symbols** in this dataset, so the question is currently moot. `minority_interest` (296 symbols) is mapped separately, so the consolidated figure can be reconstructed if needed.

### `depreciation_amortization` — four tags, all needed

| Concept | Symbols | Priority |
|---|---|---|
| DepreciationDepletionAndAmortization | 391 | 1 |
| DepreciationAmortizationAndAccretionNet | 61 | 2 |
| DepreciationAndAmortization | 57 | 3 |
| Depreciation | 188 | 4 |

`Depreciation` is placed last despite 188 symbols because it usually excludes amortization — a narrower quantity. But **53 symbols report it and no DDA-family tag at all**, so it is kept as a final fallback. Union coverage: 483.

### `gross_profit` — synthetic tag included

`GrossProfit` has 186 symbols; `GrossProfit_Calculated` (edgartools-derived, `revenue − cost_of_revenue`) adds 107, reaching 293. The synthetic value is arithmetically sound and clearly labelled, so it is kept at priority 2.

Alternative if you dislike synthetics: drop the row and derive `gross_profit` in `int_fundamental_ratios` from `revenue − cost_of_revenue` directly — coverage would be bounded by `cost_of_revenue` (328).

### `interest_expense` — three tags

`InterestExpense` (349) → `InterestExpenseNonoperating` (223) → `InterestExpenseDebt` (63). Union: **411**. These are alternative presentations of the same quantity; priority follows coverage.

---

## 6. Rejected concepts

| Concept | Symbols | Reason |
|---|---|---|
| `InterestRevenueExpenseNet` | 12 | `IS_TOTAL = true` but users are IBM, LOW, TJX, PFE — an incidental net-interest line, not revenue. See §3.3 |
| `SalesRevenueNet` | 1 | Deprecated pre-ASC-606 element |
| `InsuranceServicesRevenue` | 4 | Segment component, not top line |
| `RevenueNotFromContractWithCustomerOther` | 6 | Partial component, `IS_TOTAL = false` |
| `OtherCostOfOperatingRevenue` | 10 | A cost; matched `%revenue%` but belongs nowhere near `revenue` |
| `NetIncomeLoss` *(cashflow)* | 477 | Cross-statement duplicate — mapped from `income` only |
| `IncomeTaxExpenseBenefit` *(cashflow)* | 495 | as above |
| `Goodwill` *(cashflow)* | 435 | Balance-sheet fact leaking into the cash-flow table |
| `ProfitLoss` | 348 (CF only) | Consolidated net income including NCI; `NetIncomeLoss` is the parent-attributable figure the ratios need |
| `CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents` | 474 | Includes restricted cash and adds **0** symbols over `CashAndCashEquivalentsAtCarryingValue` |
| `PaymentsToAcquireProductiveAssets`, `PaymentsForCapitalImprovements`, `PaymentsToAcquireOtherPropertyPlantAndEquipment` | 0 | Absent from this dataset |
| `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest` | 0 | Absent |
| ~250 others | < 15 each | Long tail: impairments, pension, FX, insurance-specific, derivative gains. None feeds a planned feature. |

---

## 7. Known limitations

### 7.1 No period-end share count — `market_cap` uses a proxy

`docs/warehouse/data-dictionary.md` specifies `dei:EntityCommonStockSharesOutstanding`. **It is not ingested.** Neither is `CommonStockSharesOutstanding` nor `CommonStockSharesIssued`. The only share counts available are the weighted averages used for EPS.

The proxy is sound — `NetIncomeLoss / EarningsPerShareBasic` versus reported weighted-average shares:

| Symbol | Quarter | Implied | Reported | Error |
|---|---|---|---|---|
| A | Q1 2022 | 0.301B | 0.301B | 0.0% |
| A | Q1 2024 | 0.292B | 0.293B | 0.2% |
| A | Q1 2025 | 0.284B | 0.285B | 0.4% |

Use `shares_diluted` (488 symbols) for `market_cap`. It is a period **average**, not a period-end snapshot, so it lags real buybacks and issuance by up to a quarter. For `shares_change_yoy` this is arguably preferable — averages are less noisy than point-in-time counts.

→ Follow-up: ingest `dei:EntityCommonStockSharesOutstanding`.

### 7.2 `total_liabilities` needs a derivation fallback

`Liabilities` is tagged by only 357 symbols (6,814 of 9,760 pairs). `Assets` (496) and `StockholdersEquity` (477) are near-universal:

```
total_liabilities = coalesce(
    liabilities,
    total_assets - total_equity - coalesce(minority_interest, 0)
)
```

recovers **2,633 more pairs** — 97% coverage. The fallback belongs in `int_fundamentals_wide`, not this seed, because a seed maps tags and cannot express arithmetic.

### 7.3 ~0.5% of rows carry unit-scaling errors

| Check | Result |
|---|---|
| `EarningsPerShareBasic` outside [−50, 50] | 46 of 9,308 rows (0.5%) |
| Weighted-average shares < 1e6 | 22 of 7,222 rows (0.3%) |

Worst cases: `MCD Q4 2025` reports EPS **3,033,361** and shares **709.1**; `CHD` reports **242.6** shares. The filer used millions where units were expected and edgartools passed it through unchanged.

The seed cannot fix this. `stg_financials_long` needs a sanity filter — null out EPS outside ±$500 and share counts under 1e6 — and Phase 4 winsorization must run before any z-scoring.

### 7.4 Amendments resolve by ingest order, not filing date

EDGAR ingestion carries no `filed_date`, so "latest amendment wins" is approximated as "latest `EXECUTION_ID` wins". If a restatement is ingested before the original, the wrong value survives. Unlikely with `full_load: true` (each run re-pulls everything), but not impossible.

### 7.5 Coverage is a moving target

These numbers describe the landing tables on 2026-09-03. Index membership changes, filers switch tags, and new quarters land. Re-run the coverage query after any significant ingestion change — it is the regression test for this seed.

---

## 8. Formulas this seed enables

| Feature | Formula | Coverage bound |
|---|---|---|
| `net_margin` | `net_income / revenue` | 477 |
| `gross_margin` | `gross_profit / revenue` | 293 |
| `operating_margin` | `operating_income / revenue` | 398 |
| `roe` | `net_income_ttm / total_equity` | 477 |
| `roa` | `net_income_ttm / total_assets` | 477 |
| `market_cap` | `close × shares_diluted` | 488 — see §7.1 |
| `price_to_earnings` | `close / eps_ttm` | 489 |
| `price_to_sales` | `market_cap / revenue_ttm` | 488 |
| `price_to_book` | `market_cap / total_equity` | 477 |
| `fcf_yield` | `(ocf_ttm + capex_ttm) / market_cap` | 357 — `capex` is sign-negative |
| `ev_to_ebitda` | `(market_cap + total_debt − cash) / (operating_income + d_and_a)` | 357 |
| `debt_to_equity` | `total_debt / total_equity` | 359 |
| `debt_to_market_cap` | `total_debt / market_cap` | 359 |
| `current_ratio` | `current_assets / current_liabilities` | 416 |
| `interest_coverage` | `operating_income / interest_expense` | 398 |
| `net_debt_to_ebitda` | `(total_debt − cash) / ebitda` | 359 |
| **`accruals`** | `(net_income_ttm − ocf_ttm) / total_assets` | 477 |
| `ocf_to_net_income` | `ocf_ttm / net_income_ttm` | 477 |
| `asset_turnover` | `revenue_ttm / total_assets` | 491 |
| `capex_to_revenue` | `abs(capex_ttm) / revenue_ttm` | 357 |
| `shares_change_yoy` | `shares_diluted / lag(shares_diluted, 4) − 1` | 488 |

`accruals` uses the cash-flow approach from [Sloan (1996)](https://quantpedia.com/strategies/accrual-anomaly). The original balance-sheet formulation (changes in non-cash working capital less depreciation, over average total assets) needs stable period-over-period working-capital components, which sit at 200–263 symbols here. The cash-flow approach is the standard simplification and runs on fields at 477+ coverage.

`total_debt = long_term_debt + long_term_debt_current`, excluding leases — see §5.

Guard every division with `nullif(denominator, 0)`.

---

## 9. Extending the seed

1. **Find candidates by search, not by browsing frequency.** Frequency ranking will not group synonyms:
   ```sql
   select "CONCEPT", count(distinct "SYMBOL") syms, bool_or("IS_TOTAL") tot, "SECTION"
   from public.balance_sheet_stmts_quarterly
   where "IS_ABSTRACT" = false and "VALUE" is not null
     and "CONCEPT" ilike '%<pattern>%'
   group by 1, 4 order by 2 desc;
   ```
   Cast wide — `%debt%` alone would have missed leases and borrowings.

2. **Apply the §3 rules in order.**

3. **Measure the ceiling.** Add tags until symbol coverage plateaus, then stop.

4. **Check for collisions.** If two tags can appear for the same `(symbol, period)`, priority is mandatory:
   ```sql
   select count(*) from (
     select "SYMBOL", "QTR" from public.<table>
     where "CONCEPT" in (<your tags>) and "VALUE" is not null
     group by 1,2 having count(distinct "CONCEPT") > 1) x;
   ```

5. **Reload and re-verify.**
   ```bash
   cd src/transformation/aurum_dwh
   uv run --group dbt dbt seed --select concept_map
   ```

Fields most likely to need work next: `%cash%` (restricted-cash variants), `%payable%`, and financial-sector tags if the universe ever extends beyond the S&P 500.

---

## 10. Sources

- [DQC_0067 — Mixing legacy and ASC 606 revenue elements](https://github.com/DataQualityCommittee/dqc_us_rules/blob/master/docs/DQC_US_0067/DQC_0067.md) — FASB Data Quality Committee
- [Specific Non-controlling Interest Elements](https://xbrl.us/guidance/specific-non-controlling-interest-elements/) — XBRL US
- [FASB GAAP Taxonomy Implementation Guide — Revenue](https://xbrl.fasb.org/impguidance/Rev2_TIG/revenue_2.pdf)
- [Leveraging ASC 842 accounting for leases](https://www.usbank.com/corporate-and-commercial-banking/insights/credit-finance/equipment/leveraging-asc-842-accounting-leases.html) — operating leases are operating liabilities, not debt
- [ASC 842 impacts on financial ratios and covenants](https://ileasepro.com/blog/asc842-impacts-on-financial-ratios-and-covenants/)
- [Accrual Anomaly — Sloan (1996)](https://quantpedia.com/strategies/accrual-anomaly)
- [Sloan Accruals Ratio — formula and worked example](https://breakingdownfinance.com/finance-topics/equity-valuation/sloan-accruals-ratio/)
