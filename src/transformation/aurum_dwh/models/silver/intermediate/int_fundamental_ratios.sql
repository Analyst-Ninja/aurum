{#
    Derived fundamentals. Same grain as int_fundamentals_wide:
    (symbol, period_type, fiscal_period_end).

    Four families, all price-free:
      profitability - margins, ROE, ROA, ROIC
      growth        - QoQ and YoY on revenue, EPS, net income, OCF, share count
      leverage      - D/E, current ratio, interest coverage, net debt / EBITDA
      quality       - accruals, OCF/NI, asset turnover, capex intensity

    Valuation ratios (P/E, P/B, FCF yield, EV/EBITDA) are NOT here. They need a
    price, they move every trading day, and they therefore belong to
    int_features_daily, where the as-of join has already attached one.

    Every denominator is wrapped in nullif(..., 0). A ratio against a vanished
    denominator must be NULL - never an error, and never an infinity that
    poisons a cross-sectional z-score downstream.

    The whole wide model is carried through with `w.*` so int_features_daily has
    one dependency instead of two; it needs shares_outstanding and the TTM flows
    for valuation anyway.

    MATERIALIZATION: table. Same reasoning as int_fundamentals_wide - the YoY
    lags reach back four quarters, and the model is small.
#}

{{
    config(
        materialized='table'
    )
}}

{#
    (source column, output prefix). Growth is computed on the RAW periodic
    value, not the TTM: year-over-year against the same quarter a year ago is
    already seasonality-free, and TTM growth is a smoothed duplicate of it.
#}
{% set growth_items = [
    ('revenue',             'revenue'),
    ('eps',                 'eps'),
    ('net_income',          'net_income'),
    ('operating_cash_flow', 'ocf'),
    ('shares_outstanding',  'shares_change')
] %}

with

wide as (

    select * from {{ ref('int_fundamentals_wide') }}

),

lagged as (

    {#
        Narrowed to the grain plus what the growth expressions need. Selecting
        w.* here would drag the lag scratch columns into the final output, and
        Postgres has no `select * except`.
    #}
    select
        symbol,
        period_type,
        fiscal_period_end,

        {#
            Period-end lags exist only to validate the value lags below. A
            company that skips a filing produces a lag(1) sitting two quarters
            back; comparing against it would report a six-month change as a
            quarterly one.
        #}
        lag(fiscal_period_end, 1) over w_period as prev_1_period_end,
        lag(fiscal_period_end, 4) over w_period as prev_4_period_end,

        {% for column, prefix in growth_items %}
        {{ column }}                       as curr_{{ prefix }},
        lag({{ column }}, 1) over w_period  as prev_1_{{ prefix }},
        lag({{ column }}, 4) over w_period  as prev_4_{{ prefix }}{{ "," if not loop.last }}
        {% endfor %}

    from wide

    window w_period as (
        partition by symbol, period_type
        order by fiscal_period_end
    )

),

growth as (

    select
        symbol,
        period_type,
        fiscal_period_end,

        {% for column, prefix in growth_items %}
        {#
            QoQ: defined only for quarterly rows, and only when the previous
            row really is the previous quarter (60..120 days back).
        #}
        case
            when period_type = 'quarterly'
             and (fiscal_period_end - prev_1_period_end) between 60 and 120
                then (curr_{{ prefix }} - prev_1_{{ prefix }})
                     / nullif(abs(prev_1_{{ prefix }}), 0)
        end as {{ prefix }}_growth_qoq,

        {#
            YoY: four rows back for quarterly, one row back for annual, and in
            both cases the comparison period must sit 300..430 days earlier.

            abs() in the denominator, not the raw value: a swing from -10 to +5
            is a 150% improvement, but dividing by a negative base flips the
            sign and reports it as a collapse.
        #}
        case
            when period_type = 'quarterly'
             and (fiscal_period_end - prev_4_period_end) between 300 and 430
                then (curr_{{ prefix }} - prev_4_{{ prefix }})
                     / nullif(abs(prev_4_{{ prefix }}), 0)
            when period_type = 'annual'
             and (fiscal_period_end - prev_1_period_end) between 300 and 430
                then (curr_{{ prefix }} - prev_1_{{ prefix }})
                     / nullif(abs(prev_1_{{ prefix }}), 0)
        end as {{ prefix }}_growth_yoy{{ "," if not loop.last }}
        {% endfor %}

    from lagged

),

final as (

    select
        w.*,

        {% for column, prefix in growth_items %}
        g.{{ prefix }}_growth_qoq,
        g.{{ prefix }}_growth_yoy,
        {% endfor %}

        -- ---------------------------------------------------------------
        -- Profitability. TTM numerator over TTM denominator throughout, so a
        -- seasonal quarter cannot masquerade as a margin change.
        -- ---------------------------------------------------------------

        w.gross_profit_ttm     / nullif(w.revenue_ttm, 0)      as gross_margin,
        w.operating_income_ttm / nullif(w.revenue_ttm, 0)      as operating_margin,
        w.net_income_ttm       / nullif(w.revenue_ttm, 0)      as net_margin,

        {#
            Point-in-time equity and assets against twelve months of earnings.
            The textbook version uses the average balance over the year; that
            needs a fifth balance-sheet lag for a second-order correction and
            only buys extra NULLs at the start of each symbol history.
        #}
        w.net_income_ttm       / nullif(w.total_equity, 0)     as roe,
        w.net_income_ttm       / nullif(w.total_assets, 0)     as roa,

        {#
            ROIC = NOPAT / invested capital.

            The effective tax rate is derived per period and clamped to
            [0, 0.60]: loss-making periods imply negative or absurd rates, and
            an unclamped rate turns a tax benefit into a bogus earnings boost.
            Falls back to the 21% US statutory rate.

            Invested capital = equity + total debt - cash. Excess cash is not
            capital the operating business is earning a return on.
        #}
        (w.operating_income_ttm
            * (1 - coalesce(
                    least(greatest(w.tax_expense_ttm / nullif(w.pretax_income_ttm, 0), 0), 0.60),
                    0.21)))
            / nullif(w.total_equity + w.total_debt - coalesce(w.cash_and_equivalents, 0), 0)
                                                               as roic,

        -- ---------------------------------------------------------------
        -- Leverage and balance-sheet health.
        -- ---------------------------------------------------------------

        w.total_debt     / nullif(w.total_equity, 0)           as debt_to_equity,
        w.current_assets / nullif(w.current_liabilities, 0)    as current_ratio,

        {#
            abs() on interest expense: filers tag it both ways round, and a
            negative denominator would invert the meaning of "coverage".
        #}
        w.operating_income_ttm
            / nullif(abs(w.interest_expense_ttm), 0)           as interest_coverage,

        {#
            Net debt nets out short-term investments as well as cash - both are
            liquid enough to retire debt with. Negative net debt (a net-cash
            balance sheet) is meaningful and kept as a negative ratio.
        #}
        (w.total_debt
            - coalesce(w.cash_and_equivalents, 0)
            - coalesce(w.short_term_investments, 0))
            / nullif(w.ebitda_ttm, 0)                          as net_debt_to_ebitda,

        -- ---------------------------------------------------------------
        -- Quality. The block most often skipped, and the one carrying the most
        -- documented cross-sectional signal.
        -- ---------------------------------------------------------------

        {#
            Sloan accrual anomaly: the wedge between reported earnings and the
            cash that actually arrived, scaled by assets. Large positive
            accruals predict underperformance.
        #}
        (w.net_income_ttm - w.operating_cash_flow_ttm)
            / nullif(w.total_assets, 0)                        as accruals,

        w.operating_cash_flow_ttm
            / nullif(abs(w.net_income_ttm), 0)                 as ocf_to_net_income,

        w.revenue_ttm / nullif(w.total_assets, 0)              as asset_turnover,

        {#
            capex_ttm is negative (cash outflow), so it is negated to express
            intensity as a positive fraction of revenue.
        #}
        (-w.capex_ttm) / nullif(w.revenue_ttm, 0)              as capex_to_revenue,

        {#
            FCF on a TTM basis, built from the two TTM legs rather than by
            rolling free_cash_flow itself, so each leg keeps its own
            four-quarter completeness guard. capex_ttm is negative: this is an
            addition. int_features_daily divides it by market cap for fcf_yield.
        #}
        (w.operating_cash_flow_ttm + w.capex_ttm)              as free_cash_flow_ttm

    from wide w
    inner join growth g
        on  w.symbol            = g.symbol
        and w.period_type       = g.period_type
        and w.fiscal_period_end = g.fiscal_period_end

)

select * from final
