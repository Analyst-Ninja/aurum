{#
    The as-of join. Grain: (symbol, date).

    Every trading day gets the most recent fundamental row the market could
    ACTUALLY have known on that day - the newest row whose `available_from` is
    on or before the bar date. This is the single place look-ahead bias enters
    a quant pipeline, so the join is written to make leaking impossible rather
    than merely unlikely, and tests/assert_no_fundamental_lookahead.sql asserts
    it on every build.

    It also adds the valuation ratios, which live here rather than in
    int_fundamental_ratios because they need a price: a quarterly fundamental
    over a daily close means market_cap, P/E and FCF yield move EVERY day even
    though the fundamental input only changes four times a year. That is
    intended, not a bug.

    IMPLEMENTATION NOTE - deviation from the issue text. GH-35 specifies
    `distinct on (symbol, date)` over the candidate fundamental rows. That is
    correct but quadratic here: ~2.9M bars times ~60 candidate periods per
    symbol is a ~180M-row intermediate to sort. This uses the equivalent
    interval formulation instead - lead() turns each fundamental row
    available_from into a half-open validity window [available_from,
    next available_from), and the join becomes a range lookup that matches
    exactly one row by construction. Same result, one pass.
#}

{{
    config(
        materialized='incremental',
        unique_key=['symbol', 'date'],
        incremental_strategy='delete+insert'
    )
}}

with

bars as (

    select
        symbol,
        date,
        adj_close,
        adj_factor,
        adj_volume,
        dollar_volume
    from {{ ref('stg_ohlcv_daily') }}

    {% if is_incremental() %}
    {#
        Same lookback rule as int_technicals_daily. The only rolling feature
        here is turnover_21d, but the range is rewritten wholesale by
        delete+insert so a late-arriving fundamental restates the days it
        should have been visible on rather than only the days since the last run.
    #}
    where date >= (
        select coalesce(max(date), '1900-01-01'::date)
             - interval '{{ var("window_lookback_days") }} days'
        from {{ this }}
    )
    {% endif %}

),

with_volume as (

    select
        *,
        avg(adj_volume) over (
            partition by symbol order by date rows between 20 preceding and current row
        ) as avg_volume_21d
    from bars

),

fundamentals as (

    {#
        One fundamental row per (symbol, available_from). Quarterly and annual
        filings for the same fiscal year can land on the same visibility date;
        the quarterly row wins because it is the more recent economic period.
        Rows with a NULL available_from cannot be safely dated and are dropped
        rather than defaulted - a missing feature beats a leaked one.
    #}
    select distinct on (symbol, available_from) *
    from {{ ref('int_fundamental_ratios') }}
    where available_from is not null
    order by
        symbol,
        available_from,
        case when period_type = 'quarterly' then 0 else 1 end

),

validity as (

    select
        *,
        {#
            The next filing visibility date closes this one window. NULL on
            the latest row means "still current", which is what keeps today
            bars attached to the most recent filing.
        #}
        lead(available_from) over (
            partition by symbol order by available_from
        ) as available_to
    from fundamentals

),

joined as (

    select
        b.symbol,
        b.date,
        b.adj_close,
        b.adj_factor,
        b.adj_volume,
        b.dollar_volume,
        b.avg_volume_21d,

        {#
            LEFT join: a symbol early history predates its first filing
            available_from, and those bars must survive with NULL fundamentals
            rather than vanish. Dropping them would silently truncate the panel.
        #}
        {{ dbt_utils.star(from=ref('int_fundamental_ratios'), relation_alias='f', except=['symbol']) }},
        f.available_to as fundamental_available_to

    from with_volume b
    left join validity f
        on  f.symbol = b.symbol

        {#
            Half-open interval. `>=` on the lower bound makes the fundamental
            usable ON its availability date; `<` on the upper bound hands the
            day over to the next filing the moment that one becomes visible.
            Exactly one row can satisfy both, so this cannot fan out.
        #}
        and b.date >= f.available_from
        and (f.available_to is null or b.date < f.available_to)

),

final as (

    select
        *,

        {#
            Fundamental staleness in days. A useful feature in its own right -
            a number 85 days old carries less information than one from last
            week - and the diagnostic that makes a broken lag visible: it can
            never be negative, and a run of values past ~130 means a filing was
            missed.
        #}
        (date - available_from) as days_since_available,

        {#
            RAW close, reconstructed as adj_close / adj_factor.

            This matters. adj_close is back-adjusted for splits AND dividends,
            so it is not the price anyone paid on that date, while
            shares_outstanding and eps come from the filing as reported at the
            time. Multiplying an adjusted price by an unadjusted share count
            would understate every historical market cap by the cumulative
            adjustment factor. Valuation ratios are the one place the raw price
            is the correct input; every technical feature stays on the adjusted
            series.
        #}
        (adj_close / nullif(adj_factor, 0))                       as close_raw,

        -- ---------------------------------------------------------------
        -- Valuation. Quarterly fundamentals over a daily price.
        -- ---------------------------------------------------------------

        (adj_close / nullif(adj_factor, 0)) * shares_outstanding  as market_cap,

        {#
            Negative for a loss-making company, which is the honest answer -
            clamping it to NULL would hide exactly the names a quality factor
            wants to short. earnings_yield is the better-behaved inverse and is
            what cross-sectional ranking should generally use.
        #}
        (adj_close / nullif(adj_factor, 0)) / nullif(eps_ttm, 0)  as price_to_earnings,
        eps_ttm / nullif(adj_close / nullif(adj_factor, 0), 0)    as earnings_yield,

        (adj_close / nullif(adj_factor, 0)) * shares_outstanding
            / nullif(revenue_ttm, 0)                              as price_to_sales,

        (adj_close / nullif(adj_factor, 0)) * shares_outstanding
            / nullif(total_equity, 0)                             as price_to_book,

        free_cash_flow_ttm
            / nullif((adj_close / nullif(adj_factor, 0)) * shares_outstanding, 0)
                                                                  as fcf_yield,

        {#
            Enterprise value: what an acquirer pays for the operating business -
            equity plus the debt assumed, less the cash that comes with it.
        #}
        ((adj_close / nullif(adj_factor, 0)) * shares_outstanding
            + coalesce(total_debt, 0)
            - coalesce(cash_and_equivalents, 0)
            - coalesce(short_term_investments, 0))                as enterprise_value,

        ((adj_close / nullif(adj_factor, 0)) * shares_outstanding
            + coalesce(total_debt, 0)
            - coalesce(cash_and_equivalents, 0)
            - coalesce(short_term_investments, 0))
            / nullif(ebitda_ttm, 0)                               as ev_to_ebitda,

        {#
            Leverage against market value rather than book. Book equity goes
            stale between filings and can be negative after large buybacks;
            market cap cannot.
        #}
        total_debt
            / nullif((adj_close / nullif(adj_factor, 0)) * shares_outstanding, 0)
                                                                  as debt_to_market_cap,

        {#
            Share turnover lives here, not in int_technicals_daily: it needs
            shares_outstanding, which is a point-in-time fundamental, and
            technicals is deliberately fundamental-free.
        #}
        avg_volume_21d / nullif(shares_outstanding, 0)            as turnover_21d

    from joined

    {% if is_incremental() %}
    {#
        Warm-up guard, same contract as int_technicals_daily. This model only
        rolling feature is turnover_21d, so its degraded region is just the
        first 20 bars of the read frame rather than the first 251 - but the
        principle is identical: never re-emit a row that was computed against a
        truncated frame.
    #}
    where date >= (
        select coalesce(max(date), '1900-01-01'::date)
             - interval '{{ var("window_rewrite_days") }} days'
        from {{ this }}
    )
    {% endif %}

)

select * from final
