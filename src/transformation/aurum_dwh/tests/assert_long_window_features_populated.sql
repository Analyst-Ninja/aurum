{#
    The incremental-lookback regression test.

    A rolling 252-day feature computed over a short incremental slice does not
    error - it returns NULL. Nothing else in the build would notice. This test
    is the tripwire: it asserts that on each symbol's most recent bar, the
    longest-window features are actually populated.

    It fails the moment someone lowers window_lookback_days below 252, switches
    int_technicals_daily from delete+insert to append, or adds a window longer
    than the lookback.

    Symbols with fewer than 300 bars of history are excluded - their 252-day
    windows are legitimately incomplete and a NULL there is correct, not a bug.
#}

with

history as (

    select
        symbol,
        count(*)  as bar_count,
        max(date) as last_date
    from {{ ref('int_technicals_daily') }}
    group by symbol

),

latest_bar as (

    select
        t.symbol,
        t.date,
        t.vol_252d,
        t.ma_200,
        t.beta_252d,
        t.max_drawdown_252d
    from {{ ref('int_technicals_daily') }} t
    inner join history h
        on  h.symbol = t.symbol
        and h.last_date = t.date
    where h.bar_count >= 300

),

symbol_failures as (

    select
        symbol,
        date,
        'int_technicals_daily' as model
    from latest_bar
    where
           vol_252d          is null
        or ma_200            is null
        or beta_252d         is null
        or max_drawdown_252d is null

),

market_failures as (

    {#
        int_market_daily is checked too, and for the same reason. Its warm-up
        edge nulls market_breadth (no complete ma_50) and market_log_ret (no
        lag predecessor), and a NULL market return silently drags beta_252d
        down with it - the two failures observed before the warm-up guard
        existed. The last 60 dates is comfortably inside any rewrite tail.
    #}
    select
        null::text as symbol,
        date,
        'int_market_daily' as model
    from {{ ref('int_market_daily') }}
    where
        date >= (select max(date) from {{ ref('int_market_daily') }}) - 60
        and (
               market_breadth is null
            or market_log_ret is null
            or market_vol_21d is null
        )

)

select * from symbol_failures
union all
select * from market_failures
