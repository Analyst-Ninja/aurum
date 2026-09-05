{#
    Price-derived features. Grain: (symbol, date).

    Everything here is a window function over stg_ohlcv_daily partitioned by
    symbol and ordered by date. Nothing joins fundamentals - this model never
    needs a point-in-time guard because a price is knowable the day it prints.

    ===================================================================
    THE INCREMENTAL LOOKBACK IS THE HIGHEST-RISK DETAIL IN THIS MODEL.
    ===================================================================

    A rolling 252-day feature computed over an incremental slice of the last
    five days returns NULL. Not an error, not a warning - a silently empty
    column that trains a model on nothing. The defence is three parts, all
    load-bearing:

      1. READ a frame longer than the longest feature - window_lookback_days
         (900 calendar days, ~621 trading days).
      2. WRITE only the window_rewrite_days tail (90 calendar days, ~62 trading
         days). The leading edge of the read frame is warm-up and its rows are
         WRONG; they must not overwrite the correct ones already in the target.
         See the guard on the final select.
      3. delete+insert over that tail rather than appending, so a row computed
         near a previous slice edge is replaced rather than kept.

    Rule for anyone adding a feature here, stated the way that survives contact
    with reality:

      - Count in TRADING days, then convert: 252 trading days is ~366 calendar
        days, and window_lookback_days is in calendar days.
      - Windows COMPOUND when nested. max_drawdown_252d is a 252-row min over a
        drawdown that is itself measured against a 252-row peak: its effective
        history is 504 trading days, not 252. That single feature is what sets
        the 900-day lookback. Sizing for 252 left it wrong by up to 0.39 on
        2,472 of 11,113 recent rows - silently, and only a full-refresh-vs-
        incremental checksum caught it.
      - Anything with an unbounded frame is banned outright. A cumulative sum
        starts at a different place in a slice than in a full refresh, so it is
        not reproducible - see obv_flow_21d for what replaced the conventional
        cumulative-OBV slope and why.

    tests/assert_long_window_features_populated.sql catches a lookback that has
    gone too short; tests/assert_intermediate_covers_all_bars.sql catches a
    rewrite tail that has.

    All rolling frames are ROWS-based (trading days), not RANGE-based (calendar
    days). "252 days" in finance means 252 bars.
#}

{{
    config(
        materialized='incremental',
        unique_key=['symbol', 'date'],
        incremental_strategy='delete+insert'
    )
}}

{% set rf_daily = var('risk_free_annual') / 252.0 %}

with

bars as (

    select
        symbol,
        date,
        adj_open,
        adj_high,
        adj_low,
        adj_close,
        adj_volume,
        dollar_volume
    from {{ ref('stg_ohlcv_daily') }}

    {% if is_incremental() %}
    where date >= (
        select coalesce(max(date), '1900-01-01'::date)
             - interval '{{ var("window_lookback_days") }} days'
        from {{ this }}
    )
    {% endif %}

),

market as (

    {#
        Joined BEFORE the windows below, not after. beta_252d and
        idio_vol_252d regress the symbol's return on the market's over the same
        252-row frame; if the market series were attached downstream of the
        window there would be nothing to regress against.
    #}
    select
        date,
        market_log_ret
    from {{ ref('int_market_daily') }}

),

joined as (

    select
        b.*,
        m.market_log_ret
    from bars b
    left join market m
        on b.date = m.date

),

returns as (

    select
        *,

        {#
            Two return conventions on purpose.

            log_ret feeds volatility, Sharpe and beta - those formulas assume
            additive-across-time returns. Simple returns feed the ret_Nd
            features and the Amihud ratio, because that is how a horizon return
            is quoted and what a model should see.
        #}
        ln(adj_close / lag(adj_close) over w_symbol)            as log_ret,

        {#
            Previous close and the raw price change, materialized as plain
            columns. Postgres forbids nesting a window function inside an
            aggregate window call, so OBV and the RSI gain/loss averages below
            cannot call lag() themselves - they read these.
        #}
        lag(adj_close) over w_symbol                            as prev_close,
        adj_close - lag(adj_close) over w_symbol                as price_diff,

        adj_close / nullif(lag(adj_close,   1) over w_symbol, 0) - 1 as ret_1d,
        adj_close / nullif(lag(adj_close,   5) over w_symbol, 0) - 1 as ret_5d,
        adj_close / nullif(lag(adj_close,  21) over w_symbol, 0) - 1 as ret_21d,
        adj_close / nullif(lag(adj_close,  63) over w_symbol, 0) - 1 as ret_63d,
        adj_close / nullif(lag(adj_close, 126) over w_symbol, 0) - 1 as ret_126d,
        adj_close / nullif(lag(adj_close, 252) over w_symbol, 0) - 1 as ret_252d,

        {#
            The canonical momentum factor: the 12-month return with the most
            recent month skipped. The skip is the whole point - the last month
            carries short-term reversal, which is a different (and opposite)
            effect, and leaving it in cancels the signal.
        #}
        lag(adj_close, 21) over w_symbol
            / nullif(lag(adj_close, 252) over w_symbol, 0) - 1   as mom_12_1,

        {#
            Overnight gap: where today opened relative to yesterday close.
        #}
        adj_open / nullif(lag(adj_close) over w_symbol, 0) - 1   as gap_overnight,

        {#
            Wilder true range. greatest() ignores NULLs, so the first bar of
            a symbol falls back to the plain high-low span rather than
            returning NULL.
        #}
        greatest(
            adj_high - adj_low,
            abs(adj_high - lag(adj_close) over w_symbol),
            abs(adj_low  - lag(adj_close) over w_symbol)
        )                                                        as true_range

    from joined

    window w_symbol as (partition by symbol order by date)

),

flows as (

    select
        *,

        {#
            Signed volume: the day share volume, positive on an up close and
            negative on a down close. The building block of the OBV family.

            Its own CTE because it reads price_diff, which is itself a window
            expression and so cannot be inlined into the rolling aggregate that
            consumes it.
        #}
        (sign(price_diff) * adj_volume) as signed_volume

    from returns

),

rolling as (

    select
        *,

        -- --------------------------------------------------------------
        -- Trend
        -- --------------------------------------------------------------
        avg(adj_close) over w_10                     as ma_10,
        avg(adj_close) over w_20                     as ma_20,
        avg(adj_close) over w_50                     as ma_50,
        avg(adj_close) over w_200                    as ma_200,

        max(adj_high) over w_252                     as high_252d,
        min(adj_low)  over w_252                     as low_252d,

        {#
            Running peak inside the trailing year. The drawdown series is built
            here and its minimum is taken in the next CTE - a window function
            cannot be nested inside another window function.
        #}
        adj_close / nullif(max(adj_close) over w_252, 0) - 1     as drawdown_252d,

        -- --------------------------------------------------------------
        -- Volatility and risk. sqrt(252) annualizes a daily stdev.
        -- --------------------------------------------------------------
        stddev_samp(log_ret) over w_21  * sqrt(252)  as vol_21d,
        stddev_samp(log_ret) over w_63  * sqrt(252)  as vol_63d,
        stddev_samp(log_ret) over w_252 * sqrt(252)  as vol_252d,

        {#
            Parkinson high-low estimator: sqrt(mean(ln(H/L)^2) / (4 ln 2)).
            It uses the whole bar range instead of two closing prints, and is
            roughly five times more efficient than close-to-close at the same
            sample size. Blind to overnight gaps, which is why both are kept.
        #}
        sqrt(
            avg(power(ln(adj_high / nullif(adj_low, 0)), 2)) over w_21
            / (4 * ln(2.0))
        ) * sqrt(252)                                as parkinson_vol_21d,

        {#
            Downside deviation: the same dispersion measure but counting only
            losses. least(log_ret, 0) zeroes the up days rather than dropping
            them, which is the Sortino convention.
        #}
        sqrt(avg(power(least(log_ret, 0), 2)) over w_21) * sqrt(252)
                                                     as downside_dev_21d,

        {#
            ATR on a simple 14-bar mean of true range. Wilders original uses
            his own recursive smoother, which Postgres has no window equivalent
            for; the simple mean tracks it closely and is what most screeners
            now report. Documented here so nobody "fixes" it silently.
        #}
        avg(true_range) over w_14                    as atr_14,

        {#
            Sharpe over the frame, annualized. rf is the risk_free_annual var
            divided by 252 to reach a daily rate, matching the sharpe_30d
            formula in docs/warehouse/data-dictionary.md.
        #}
        (avg(log_ret) over w_21 - {{ rf_daily }})
            / nullif(stddev_samp(log_ret) over w_21, 0) * sqrt(252)
                                                     as sharpe_21d,
        (avg(log_ret) over w_63 - {{ rf_daily }})
            / nullif(stddev_samp(log_ret) over w_63, 0) * sqrt(252)
                                                     as sharpe_63d,

        {#
            Beta against the equal-weight universe. regr_count is the number of
            rows where BOTH series are non-null; requiring 200 of a possible
            252 keeps a half-warm window from reporting a confident beta.
        #}
        case
            when regr_count(log_ret, market_log_ret) over w_252 >= 200
                then regr_slope(log_ret, market_log_ret) over w_252
        end                                          as beta_252d,

        {#
            Correlation to the market over the same frame. Feeds idio_vol below
            via the identity residual_sd = total_sd * sqrt(1 - r^2), which is
            exact for a univariate OLS fit and avoids a second pass to compute
            residuals row by row.
        #}
        case
            when regr_count(log_ret, market_log_ret) over w_252 >= 200
                then corr(log_ret, market_log_ret) over w_252
        end                                          as market_corr_252d,

        -- --------------------------------------------------------------
        -- Liquidity
        -- --------------------------------------------------------------
        avg(dollar_volume) over w_21                 as adv_21d,

        (adj_volume - avg(adj_volume) over w_21)
            / nullif(stddev_samp(adj_volume) over w_21, 0)
                                                     as volume_zscore_21d,

        {#
            Amihud illiquidity: average price impact per dollar traded. Values
            are tiny in absolute terms (~1e-11) - that is expected, the feature
            is only ever used cross-sectionally after ranking or z-scoring.
        #}
        avg(abs(ret_1d) / nullif(dollar_volume, 0)) over w_21
                                                     as amihud_illiq_21d,

        {#
            OBV as a bounded 21-day net flow: the share of the period volume
            that traded on up days rather than down days. Ranges [-1, 1];
            positive means accumulation.

            This replaces the conventional obv_slope_21d (the regression slope
            of cumulative OBV), which is NOT reproducible here. Cumulative OBV
            reaches ~1e11 while its 21-day variation is ~1e8, so the slope is
            computed by catastrophic cancellation and the answer depends on
            where the cumulative sum was started - which differs between a full
            refresh (2000) and an incremental slice. Measured drift was under
            1e-6, harmless numerically but enough to make the full-vs-
            incremental checksum differ on 9,062 of 11,113 rows.

            The net-flow form carries the same accumulation/distribution signal
            over a bounded frame, so it is exactly reproducible AND has a
            meaningful scale, which the raw slope did not.
        #}
        sum(signed_volume) over w_21
            / nullif(sum(adj_volume) over w_21, 0)   as obv_flow_21d,

        -- --------------------------------------------------------------
        -- Oscillators
        -- --------------------------------------------------------------
        {#
            RSI on simple 14-bar means of gains and losses (Cutler's RSI)
            rather than Wilder's recursive smoothing - same reason as ATR
            above. Bounded 0..100 either way.
        #}
        avg(greatest( price_diff, 0)) over w_14       as avg_gain_14,
        avg(greatest(-price_diff, 0)) over w_14       as avg_loss_14,

        {#
            MACD built on SIMPLE moving averages, not exponential. Postgres has
            no window EMA and a recursive CTE over ~3M rows is not viable; the
            SMA variant keeps the same fast-minus-slow trend construction. This
            is a documented deviation from the 12/26/9 EMA convention, not an
            oversight - the column is a relative trend measure either way and
            is z-scored cross-sectionally before any model sees it.
        #}
        avg(adj_close) over w_12                     as ma_12,
        avg(adj_close) over w_26                     as ma_26,

        stddev_samp(adj_close) over w_20             as sd_20,

        max(adj_high) over w_14                      as high_14d,
        min(adj_low)  over w_14                      as low_14d

    from flows

    window
        w_10  as (partition by symbol order by date rows between   9 preceding and current row),
        w_12  as (partition by symbol order by date rows between  11 preceding and current row),
        w_14  as (partition by symbol order by date rows between  13 preceding and current row),
        w_20  as (partition by symbol order by date rows between  19 preceding and current row),
        w_21  as (partition by symbol order by date rows between  20 preceding and current row),
        w_26  as (partition by symbol order by date rows between  25 preceding and current row),
        w_50  as (partition by symbol order by date rows between  49 preceding and current row),
        w_63  as (partition by symbol order by date rows between  62 preceding and current row),
        w_200 as (partition by symbol order by date rows between 199 preceding and current row),
        w_252 as (partition by symbol order by date rows between 251 preceding and current row)

),

derived as (

    select
        *,

        {#
            Deepest peak-to-trough inside the trailing year. Negative by
            construction; 0 means the symbol closed at a 252-day high today.
            Second pass because drawdown_252d is itself a window expression.
        #}
        min(drawdown_252d) over (
            partition by symbol order by date rows between 251 preceding and current row
        )                                            as max_drawdown_252d,

        {#
            MACD line and its 9-bar signal. Same second-pass reason: the signal
            is an average of the MACD line, which does not exist until this CTE.
        #}
        (ma_12 - ma_26)                              as macd,
        avg(ma_12 - ma_26) over (
            partition by symbol order by date rows between 8 preceding and current row
        )                                            as macd_signal

    from rolling

),

final as (

    select
        -- Grain.
        symbol,
        date,

        -- Momentum and trend.
        ret_1d,
        ret_5d,
        ret_21d,
        ret_63d,
        ret_126d,
        ret_252d,
        mom_12_1,
        -- Short-horizon reversal is the negated 5-day return, by definition.
        (-ret_5d)                                            as reversal_5d,
        gap_overnight,

        ma_10,
        ma_20,
        ma_50,
        ma_200,
        adj_close / nullif(ma_50,  0) - 1                    as price_to_ma_50,
        adj_close / nullif(ma_200, 0) - 1                    as price_to_ma_200,
        ma_50     / nullif(ma_200, 0) - 1                    as ma_50_over_200,

        high_252d,
        low_252d,
        -- Negative: distance BELOW the 52-week high. 0 = at the high.
        adj_close / nullif(high_252d, 0) - 1                 as dist_from_52w_high,
        adj_close / nullif(low_252d,  0) - 1                 as dist_from_52w_low,

        -- Volatility and risk.
        vol_21d,
        vol_63d,
        vol_252d,
        parkinson_vol_21d,
        downside_dev_21d,
        atr_14,
        atr_14 / nullif(adj_close, 0)                        as atr_pct,
        max_drawdown_252d,
        sharpe_21d,
        sharpe_63d,
        beta_252d,

        {#
            Idiosyncratic volatility - the part of the symbol variance the
            market does not explain. greatest(..., 0) guards the sqrt against a
            correlation that rounds marginally past 1.
        #}
        vol_252d * sqrt(greatest(1 - power(market_corr_252d, 2), 0))
                                                             as idio_vol_252d,
        market_corr_252d,

        -- Liquidity.
        adv_21d,
        volume_zscore_21d,
        amihud_illiq_21d,
        obv_flow_21d,

        -- Oscillators.
        {#
            RSI. The avg_loss = 0 case is a run of pure gains: the textbook
            limit is 100, which the ratio form cannot reach because nullif
            turns the denominator into NULL. Handled explicitly.
        #}
        case
            when avg_loss_14 is null or avg_gain_14 is null then null
            when avg_loss_14 = 0 and avg_gain_14 = 0        then 50.0
            when avg_loss_14 = 0                           then 100.0
            else 100.0 - (100.0 / (1.0 + avg_gain_14 / avg_loss_14))
        end                                                  as rsi_14,

        macd,
        macd_signal,
        (macd - macd_signal)                                 as macd_hist,

        {#
            Bollinger %B: where the close sits inside the 2-sigma band, 0 at
            the lower edge and 1 at the upper. Width is the band span relative
            to its own midline - a volatility-regime measure.
        #}
        (adj_close - (ma_20 - 2 * sd_20))
            / nullif(4 * sd_20, 0)                           as bollinger_pctb_20,
        (4 * sd_20) / nullif(ma_20, 0)                       as bollinger_width_20,

        {#
            Stochastic %K, clamped to [0, 100].

            The clamp is not cosmetic. stg_ohlcv_daily carries adj_close as
            landed but derives adj_low as low * adj_factor, so on a bar that
            closed exactly at its low the two disagree in the last float digit
            and %K lands a whisker below zero. ~3.6k rows of ~2.9M do this.
            Clamping keeps the 0-100 range the indicator is defined on, which
            the schema test then asserts rather than tolerates.

            greatest() and least() IGNORE nulls in Postgres rather than
            propagating them, so the flat-range case is short-circuited by an
            explicit case expression - clamping it directly would report a
            zero-range bar as %K = 0 instead of NULL.
        #}
        case
            when nullif(high_14d - low_14d, 0) is null then null
            else least(greatest(
                     (adj_close - low_14d) / (high_14d - low_14d) * 100,
                 0), 100)
        end                                                  as stoch_k_14,

        -- Carried through: int_features_daily prices its valuation ratios off
        -- the same bar these features were computed on.
        adj_close,
        adj_volume,
        dollar_volume

    from derived

    {% if is_incremental() %}
    {#
        THE WARM-UP GUARD - the second half of the lookback contract, and the
        part that is easy to leave out.

        `bars` above READ window_lookback_days of history. This clause controls
        what is WRITTEN, and it must be a strictly shorter tail, because the
        leading edge of that read frame is warm-up: its first bar has no lag()
        predecessor (ret_1d, log_ret and price_diff are NULL there), its first
        49 bars have no complete ma_50, its first 251 have no complete 252-day
        window. Those rows already exist in the target, computed correctly by an
        earlier run against real history.

        Without this clause, delete+insert overwrites them with the degraded
        versions - which is exactly what happened before it was added: 500
        symbols with a NULL ret_1d on the frame first date, and beta_252d
        going NULL on recent rows whose 252-day window reached back into it.
        The checksum comparison in the GH-35 acceptance criteria is what caught
        it; nothing else would have.
    #}
    where date >= (
        select coalesce(max(date), '1900-01-01'::date)
             - interval '{{ var("window_rewrite_days") }} days'
        from {{ this }}
    )
    {% endif %}

)

select * from final
