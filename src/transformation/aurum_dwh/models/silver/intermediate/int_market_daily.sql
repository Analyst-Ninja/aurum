{#
    The universe aggregate. Grain: one row per (date).

    Two jobs:
      1. the beta denominator - int_technicals_daily regresses each symbols
         daily log return on market_log_ret, so this model must be built first
         and joined BEFORE the 252-day window, not after.
      2. the regime features - market return, market volatility and market
         breadth describe what kind of tape a given day was, which is what lets
         a cross-sectional model tell a stock-specific move from a beta move.

    EQUAL WEIGHT, not cap weight. Two reasons: share counts are fundamentals and
    only known point-in-time (dragging int_fundamentals_wide into this model and
    with it the lookahead problem), and an equal-weight index is the right
    benchmark for a model that ranks across the universe rather than tracks it.

    Breadth recomputes its own 50-day moving average rather than reading
    int_technicals_daily. That duplication is deliberate: technicals depends on
    this model for beta, so reading it back would close a dependency cycle.
#}

{{
    config(
        materialized='incremental',
        unique_key='date',
        incremental_strategy='delete+insert'
    )
}}

with

bars as (

    select
        symbol,
        date,
        adj_close
    from {{ ref('stg_ohlcv_daily') }}

    {% if is_incremental() %}
    {#
        Lookback window, same rule as int_technicals_daily: the longest window
        below is 63 days and breadth needs 50, so window_lookback_days (400) is
        comfortably clear of both. The whole range is then rewritten by
        delete+insert rather than appended to.
    #}
    where date >= (
        select coalesce(max(date), '1900-01-01'::date)
             - interval '{{ var("window_lookback_days") }} days'
        from {{ this }}
    )
    {% endif %}

),

per_symbol as (

    select
        symbol,
        date,
        adj_close,

        {#
            Log returns, not simple. They aggregate additively across time,
            which is what makes market_ret_21d a plain sum below, and they are
            the convention the volatility and beta formulas assume.
            stg_ohlcv_daily guarantees adj_close > 0, so ln() is safe.
        #}
        ln(adj_close / lag(adj_close) over w_symbol) as log_ret,

        avg(adj_close) over (
            partition by symbol
            order by date
            rows between 49 preceding and current row
        ) as ma_50,

        count(*) over (
            partition by symbol
            order by date
            rows between 49 preceding and current row
        ) as ma_50_n

    from bars

    window w_symbol as (partition by symbol order by date)

),

by_date as (

    select
        date,

        {#
            Equal-weight mean of member returns. avg() skips NULLs, which is
            what should happen on each symbol first bar and on a symbol that
            has not listed yet - they contribute nothing rather than a zero.
        #}
        avg(log_ret)                                       as market_log_ret,
        exp(avg(log_ret)) - 1                              as market_ret_1d,

        {#
            Cross-sectional dispersion of the day. High dispersion days are the
            ones where stock selection pays; low dispersion days are beta days.
        #}
        stddev_samp(log_ret)                               as market_xs_dispersion,

        {#
            Breadth: share of the universe trading above its own 50-day average.
            ma_50_n = 50 excludes symbols whose average is still warming up, so
            early history does not report a spuriously extreme breadth.
        #}
        avg(case
                when ma_50_n = 50 and adj_close > ma_50 then 1.0
                when ma_50_n = 50                       then 0.0
            end)                                           as market_breadth,

        count(*)                                           as universe_size,
        count(log_ret)                                     as universe_with_return

    from per_symbol
    group by date

),

final as (

    select
        date,
        market_log_ret,
        market_ret_1d,
        market_xs_dispersion,
        market_breadth,
        universe_size,
        universe_with_return,

        {#
            Cumulative log returns sum, so an N-day market return is a rolling
            sum of daily log returns exponentiated back to a simple return.
        #}
        exp(sum(market_log_ret) over w_21) - 1             as market_ret_21d,
        exp(sum(market_log_ret) over w_63) - 1             as market_ret_63d,

        -- Annualized, matching the vol convention in int_technicals_daily.
        stddev_samp(market_log_ret) over w_21 * sqrt(252)  as market_vol_21d,
        stddev_samp(market_log_ret) over w_63 * sqrt(252)  as market_vol_63d

    from by_date

    window
        w_21 as (order by date rows between 20 preceding and current row),
        w_63 as (order by date rows between 62 preceding and current row)

)

select * from final

{% if is_incremental() %}
{#
    Warm-up guard, same contract as int_technicals_daily: read
    window_lookback_days, write only the window_rewrite_days tail. The leading
    edge of the read frame has no lag() predecessor for market_log_ret and no
    complete ma_50 behind breadth, so re-emitting it would replace correct rows
    with NULLs - it did, for 98 dates, before this clause existed.
#}
where date >= (
    select coalesce(max(date), '1900-01-01'::date)
         - interval '{{ var("window_rewrite_days") }} days'
    from {{ this }}
)
{% endif %}
