{#
    Long financial facts pivoted to one row per statement period.
    Grain: (symbol, period_type, fiscal_period_end).

    Three things happen here and nowhere else:

      1. resolve - stg_financials_long is long by CONCEPT, so several XBRL
                   concepts can compete for one canonical_name in a single
                   period (e.g. Revenues and RevenueFromContract...). The seed
                   priority column picks the winner; canonical_sign flips the
                   polarity of items EDGAR reports as positive magnitudes
                   (capex, buybacks, dividends paid) into cash-flow signing.
      2. pivot   - canonical_name becomes a column.
      3. TTM     - flow items get a 4-quarter rolling sum, and every row gets
                   `available_from`, the point-in-time visibility date.

    MATERIALIZATION: table, not incremental, deliberately. The TTM windows look
    back three quarters and the growth lags in int_fundamental_ratios look back
    four, so an incremental slice would have to re-read roughly two years of
    periods to stay correct. The whole model is ~20k rows off 880k mapped long
    rows - a full rebuild costs about two seconds. Incremental here would buy nothing and add
    the exact silent-NULL failure mode the technicals model has to defend
    against.
#}

{{
    config(
        materialized='table'
    )
}}

{#
    Canonical names promoted to columns. Superset of the list in GH-35: the
    extras (cost_of_revenue, pretax_income, tax_expense, current_assets, ...)
    are not features themselves, they are the inputs int_fundamental_ratios
    needs for ROIC, the current ratio and net debt. Anything in the seed but
    absent here was judged not to feed a ratio - see docs/concept-map-rationale.
#}
{% set pivot_columns = [
    'revenue',
    'cost_of_revenue',
    'gross_profit_reported',
    'operating_income',
    'operating_expenses',
    'net_income',
    'pretax_income',
    'tax_expense',
    'interest_expense',
    'eps_basic',
    'eps_diluted',
    'shares_basic',
    'shares_diluted',
    'total_assets',
    'total_liabilities',
    'total_equity',
    'current_assets',
    'current_liabilities',
    'cash_and_equivalents',
    'short_term_investments',
    'inventory',
    'receivables',
    'long_term_debt',
    'long_term_debt_current',
    'ppe_net',
    'goodwill',
    'retained_earnings',
    'operating_cash_flow',
    'investing_cash_flow',
    'financing_cash_flow',
    'capex',
    'depreciation_amortization',
    'share_based_comp',
    'dividends_paid',
    'buybacks',
    'dividends_per_share',
    'minority_interest'
] %}

{#
    `gross_profit_reported` above is the only aliased pivot target: the exported
    `gross_profit` below coalesces it with revenue - cost_of_revenue, so the
    filled value is what downstream sees.
#}
{% set pivot_source = {'gross_profit_reported': 'gross_profit'} %}

{#
    Flow items that get a trailing-twelve-month rolling sum. Stocks
    (total_assets, total_equity, cash) are excluded on purpose: a balance sheet
    is already a point-in-time level, and summing four of them is meaningless.
    `eps` and `ebitda` are derived below, hence their absence from the pivot.
#}
{% set ttm_columns = [
    'revenue',
    'gross_profit',
    'operating_income',
    'net_income',
    'pretax_income',
    'tax_expense',
    'interest_expense',
    'operating_cash_flow',
    'capex',
    'depreciation_amortization',
    'share_based_comp',
    'ebitda',
    'eps'
] %}

with

resolved as (

    {#
        Collision resolution. Within one (symbol, period_type, period,
        canonical_name) there may be several concepts; the seeds priority
        column orders them, 1 = most trusted. abs(value) desc and concept asc
        only break ties so the pick is stable across runs rather than dependent
        on scan order.

        value * canonical_sign is applied here so every consumer downstream
        reads cash-flow-signed numbers: capex and buybacks are negative
        (outflows), everything else keeps its reported polarity.
    #}
    select
        symbol,
        period_type,
        fiscal_period_end,
        fiscal_year,
        fiscal_quarter,
        canonical_name,
        value * canonical_sign as value,

        row_number() over (
            partition by symbol, period_type, fiscal_period_end, canonical_name
            order by concept_priority asc, abs(value) desc, concept asc
        ) as _row_num

    from {{ ref('stg_financials_long') }}

    where
            canonical_name    is not null
        and fiscal_period_end is not null
        and value             is not null

),

pivoted as (

    select
        symbol,
        period_type,
        fiscal_period_end,

        {#
            max() over a single surviving row per canonical_name is a pivot,
            not an aggregation - `resolved` is already deduplicated to one row
            per (grain, canonical_name), so there is nothing for max to choose
            between.
        #}
        max(fiscal_year)    as fiscal_year,
        max(fiscal_quarter) as fiscal_quarter,

        {% for column in pivot_columns %}
        max(case when canonical_name = '{{ pivot_source.get(column, column) }}' then value end) as {{ column }}{{ "," if not loop.last }}
        {% endfor %}

    from resolved
    where _row_num = 1
    group by symbol, period_type, fiscal_period_end

),

derived as (

    select
        symbol,
        period_type,
        fiscal_period_end,
        fiscal_year,
        fiscal_quarter,

        {% for column in pivot_columns if column != 'gross_profit_reported' %}
        {{ column }},
        {% endfor %}

        {#
            Gross profit is not always tagged; when it is missing but both legs
            are present it is exactly revenue - cost of revenue. The reported
            value always wins - a filed subtotal beats a reconstruction.
        #}
        coalesce(
            gross_profit_reported,
            revenue - cost_of_revenue
        )                                       as gross_profit,
        gross_profit_reported,

        {#
            Diluted first: it is the conservative count and the one valuation
            convention uses. Basic is the fallback for filers that omit it.
        #}
        coalesce(eps_diluted, eps_basic)        as eps,
        coalesce(shares_diluted, shares_basic)  as shares_outstanding,

        {#
            Total debt is the interest-bearing balance only - the current
            portion of long-term debt plus the non-current remainder. Payables
            and lease liabilities are deliberately excluded: D/E and
            net-debt/EBITDA are leverage measures, not total-liability ones.
        #}
        coalesce(long_term_debt, 0)
            + coalesce(long_term_debt_current, 0)                as total_debt,

        {#
            EBITDA proxied as operating income + D&A. The textbook definition
            starts from pretax income and adds back interest, but operating
            income is the more reliably tagged line and excludes the
            non-operating noise that makes cross-sectional EV/EBITDA useless.
            NULL if either leg is missing - a partial add-back is worse than
            no number.
        #}
        (operating_income + depreciation_amortization)           as ebitda,

        {#
            capex is already negative (seed sign = -1), so free cash flow is an
            addition, not a subtraction. Getting this backwards would double
            the capex charge.
        #}
        (operating_cash_flow + capex)                            as free_cash_flow

    from pivoted

),

with_ttm as (

    select
        *,

        {% for column in ttm_columns %}
        {#
            Guarded 4-quarter rolling sum.

            Three conditions, all necessary:
              - period_type = 'annual' short-circuits: an annual row already IS
                twelve months, so its TTM is itself.
              - count(...) = 4 rejects windows where any of the four quarters is
                missing the item. sum() skips NULLs silently, which would
                otherwise return a 3-quarter total labelled as twelve months.
              - the 240..320 day span rejects windows that straddle a reporting
                gap. Four consecutive quarter-ends span ~273 days; a window that
                skips a quarter spans ~365 and is not a TTM.
        #}
        case
            when period_type = 'annual'
                then {{ column }}
            when count({{ column }}) over w_ttm = 4
             and (fiscal_period_end - min(fiscal_period_end) over w_ttm) between 240 and 320
                then sum({{ column }}) over w_ttm
        end as {{ column }}_ttm{{ "," if not loop.last }}
        {% endfor %}

    from derived

    window w_ttm as (
        partition by symbol, period_type
        order by fiscal_period_end
        rows between 3 preceding and current row
    )

),

final as (

    select
        *,

        {#
            THE point-in-time guard. fiscal_period_end is when the quarter
            closed; available_from is the first date a model is allowed to know
            about it. Every join downstream keys on this column and never on
            fiscal_period_end - see the leakage tests.

            Lags come from dbt_project vars (60d quarterly, 90d annual) and sit
            past the observed ~43-day average filing gap on purpose. Replace
            with a real filed_date join once EDGAR ingestion carries one.
        #}
        case
            when period_type = 'quarterly'
                then fiscal_period_end + interval '{{ var("fundamental_lag_days_quarterly") }} days'
            when period_type = 'annual'
                then fiscal_period_end + interval '{{ var("fundamental_lag_days_annual") }} days'
        end::date as available_from

    from with_ttm

)

select * from final
