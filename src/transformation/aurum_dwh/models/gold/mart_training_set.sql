{#
    The supervised panel. Grain: (symbol, date).

    mart_features, plus the four forward-looking targets and the walk-forward
    fold id, restricted to names that were actually tradable on the day.

    This is the ONLY model in the warehouse that looks into the future, and it
    does so on purpose. Nothing reads it except training. Live inference reads
    mart_features, which has no target columns to leak.

    THE TARGETS ARE NULL AT THE RIGHT EDGE, BY CONSTRUCTION. lead() past the
    last bar returns null, and it must stay null - a coalesce to zero here
    would hand the model a five-day flat return as a real observation on every
    recent row, and those are exactly the rows a fresh backtest scores.
    tests/assert_targets_null_at_edge.sql enforces it.
#}

{{
    config(
        materialized='table'
    )
}}

with

panel as (

    {#
        Forward returns are taken on the FULL panel, before the tradability
        filter. Filtering first would make lead(adj_close, 5) skip over the
        days a symbol spent below the price or liquidity floor and silently
        stretch a 5-day horizon into 8 or 12 calendar sessions.

        Log returns, matching the convention used throughout silver: they are
        additive across time and symmetric around zero, which is what a
        regression target should be. adj_close is guaranteed positive by
        stg_ohlcv_daily, so ln() is safe.
    #}
    select
        *,
        ln(lead(adj_close,  5) over w_symbol / adj_close)                 as fwd_ret_5d,
        ln(lead(adj_close, 21) over w_symbol / adj_close)                 as fwd_ret_21d
    from {{ ref('mart_features') }}
    window w_symbol as (partition by symbol order by date)

),

tradable as (

    {#
        Untradable names are dropped rather than flagged. A sub-dollar stock or
        one turning over less than min_adv_usd a day cannot be entered at the
        price the backtest assumes, so training on it buys signal that cannot
        be harvested - the classic way a paper strategy beats the market and a
        live one does not.

        close_raw, not adj_close: the price filter asks what the share actually
        cost that day, and adj_close is back-adjusted for every later split and
        dividend, so a stock that traded at 4 dollars in 2015 can carry an
        adj_close of 40 cents today and be dropped for the wrong reason.
    #}
    select *
    from panel
    where close_raw >= {{ var('min_price') }}
      and adv_21d   >= {{ var('min_adv_usd') }}

),

final as (

    select
        t.*,

        {#
            Excess over the universe that day. Removes the market move, which
            is the part of a forward return no cross-sectional model can
            predict and every symbol shares. Computed on the TRADABLE universe,
            because that is the set the model actually ranks and the set a
            long-short book could hold.
        #}
        t.fwd_ret_5d - avg(t.fwd_ret_5d) over w_date                      as fwd_ret_5d_excess,

        {#
            The ranking label. Same null-safe rank construction as the feature
            deciles in mart_features: ntile() would bucket the null right edge
            along with the real returns.
        #}
        case
            when t.fwd_ret_5d is null then null
            else least(10,
                     floor(
                         (rank() over (partition by t.date order by t.fwd_ret_5d) - 1)
                         * 10.0 / nullif(count(t.fwd_ret_5d) over w_date, 0)
                     )::int + 1)
        end                                                               as fwd_ret_5d_xs_decile,

        {#
            Binary label. Null propagates from fwd_ret_5d rather than
            collapsing to 0, so a classifier trained on this drops the edge
            rows instead of learning that the future is always flat.
        #}
        case
            when t.fwd_ret_5d is null then null
            when t.fwd_ret_5d > 0     then 1
            else 0
        end                                                               as label_up_5d,

        {#
            WALK-FORWARD FOLDS, ONE PER CALENDAR MONTH. NEVER RANDOM.

            A random split puts tomorrow in the training set and today in the
            validation set. Since neighbouring days share overlapping feature
            windows and overlapping forward returns, the model memorises the
            answer and the validation score is fiction.

            dense_rank over the month makes fold_id monotone in time, so
            training on fold_id <= k and validating on fold_id = k + 1 is a
            correct expanding-window split with no gap to reason about. It is
            non-null on every row, which the schema test asserts.
        #}
        dense_rank() over (order by date_trunc('month', t.date))           as fold_id

    from tradable t
    window w_date as (partition by t.date)

)

select * from final
