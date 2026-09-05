{#
    THE feature store. Grain: (symbol, date).

    Everything a model may look at, and NOTHING it may not: this model carries
    no forward return, no label, no fold. That is a structural guarantee, not a
    convention - live inference reads this table, and a target column here
    would be a target column in production. Targets live one model downstream,
    in mart_training_set.

    Three inputs, joined on the bar:
      int_technicals_daily  - price, volatility, liquidity, oscillators
      int_features_daily    - point-in-time fundamentals and valuation ratios
      int_market_daily      - the regime columns, one row per date
    plus stg_companies for the sector, which the cross-sectional block needs.

    The join to stg_companies is INNER, not LEFT. Gold is the S&P 500 panel by
    definition, a symbol with no sector cannot get a _vs_sector value, and the
    relationships test in _gold_models.yml asserts exactly this. A symbol that
    disappears from the seed drops out of gold rather than sitting in it with a
    null sector.

    MATERIALIZATION: table, and a full rebuild rather than incremental. The
    cross-sectional block below is a whole-panel computation - every z-score
    and decile depends on every other symbol on that date - so an incremental
    tail would have to re-read the full date anyway. See dbt_project.yml.
#}

{{
    config(
        materialized='table'
    )
}}

{#
    ---------------------------------------------------------------------
    THE CROSS-SECTIONAL FEATURE LIST
    ---------------------------------------------------------------------

    Each name below gets three derived columns, computed per date across the
    universe:

      _z          winsorized at the 1st/99th percentile of that date, then
                  z-scored. Winsorize BEFORE the z-score: one bad tick
                  otherwise drags the mean and the standard deviation of the
                  whole cross-section, and every other symbol z moves with it.
      _decile     rank within that date, 1 = lowest, 10 = highest.
      _vs_sector  raw value minus the median of its sector on that date.

    This is what turns per-stock indicators into a panel a ranking model can
    learn from. A P/E of 30 means nothing absolute; top decile P/E within tech,
    today, means something.

    The list is CURATED rather than every numeric column, because each entry
    costs three window passes over ~3M rows and the panel is rebuilt in full.
    Near-duplicates are dropped in favour of one representative:
    price_to_earnings is out and earnings_yield is in (the inverse is
    well-behaved through zero earnings, the ratio explodes), ret_5d is out
    because reversal_5d is its negation, ma_50 levels are out because
    price_to_ma_50 is the scale-free version of the same thing.

    To add a feature: put its name here, and it must exist in `base` below.
#}
{% set xs_features = [
    'ret_21d',
    'ret_63d',
    'ret_126d',
    'mom_12_1',
    'reversal_5d',
    'price_to_ma_50',
    'dist_from_52w_high',

    'vol_21d',
    'vol_252d',
    'max_drawdown_252d',
    'sharpe_63d',
    'beta_252d',
    'idio_vol_252d',

    'adv_21d',
    'amihud_illiq_21d',
    'turnover_21d',

    'rsi_14',
    'macd_hist',
    'bollinger_pctb_20',

    'market_cap',
    'earnings_yield',
    'price_to_sales',
    'price_to_book',
    'fcf_yield',
    'ev_to_ebitda',

    'gross_margin',
    'net_margin',
    'roe',
    'roic',

    'revenue_growth_yoy',
    'eps_growth_yoy',
    'shares_change_growth_yoy',

    'debt_to_equity',
    'net_debt_to_ebitda',

    'accruals',
    'asset_turnover'
] %}

with

calendar as (

    {#
        Calendar regime flags, derived from the TRADING calendar rather than
        the civil one: is_month_end marks the last date the market was open in
        that month, which is when month-end rebalancing flows actually land.
        int_market_daily is already one row per traded date, so it is the
        cheapest possible source for this.
    #}
    select
        date,
        extract(isodow from date)::int                                    as day_of_week,
        extract(month  from date)::int                                    as month_of_year,
        (date = max(date) over (partition by date_trunc('month',   date)))::int
                                                                          as is_month_end,
        (date = max(date) over (partition by date_trunc('quarter', date)))::int
                                                                          as is_quarter_end
    from {{ ref('int_market_daily') }}

),

base as (

    select
        -- ---------------------------------------------------------------
        -- Grain and dimensions.
        -- ---------------------------------------------------------------
        t.symbol,
        t.date,
        c.sector,
        c.industry,

        -- ---------------------------------------------------------------
        -- Price. adj_close drives every technical; close_raw is the
        -- unadjusted price the valuation ratios were computed against and the
        -- one the tradability filter in mart_training_set screens on.
        -- ---------------------------------------------------------------
        t.adj_close,
        f.close_raw,
        t.adj_volume,
        t.dollar_volume,

        -- ---------------------------------------------------------------
        -- Momentum and trend.
        -- ---------------------------------------------------------------
        t.ret_1d,
        t.ret_5d,
        t.ret_21d,
        t.ret_63d,
        t.ret_126d,
        t.ret_252d,
        t.mom_12_1,
        t.reversal_5d,
        t.gap_overnight,
        t.ma_10,
        t.ma_20,
        t.ma_50,
        t.ma_200,
        t.price_to_ma_50,
        t.price_to_ma_200,
        t.ma_50_over_200,
        t.high_252d,
        t.low_252d,
        t.dist_from_52w_high,
        t.dist_from_52w_low,

        -- ---------------------------------------------------------------
        -- Volatility and risk.
        -- ---------------------------------------------------------------
        t.vol_21d,
        t.vol_63d,
        t.vol_252d,
        t.parkinson_vol_21d,
        t.downside_dev_21d,
        t.atr_14,
        t.atr_pct,
        t.max_drawdown_252d,
        t.sharpe_21d,
        t.sharpe_63d,
        t.beta_252d,
        t.idio_vol_252d,
        t.market_corr_252d,

        -- ---------------------------------------------------------------
        -- Liquidity and volume.
        -- ---------------------------------------------------------------
        t.adv_21d,
        t.volume_zscore_21d,
        t.amihud_illiq_21d,
        t.obv_flow_21d,
        f.turnover_21d,

        -- ---------------------------------------------------------------
        -- Oscillators.
        -- ---------------------------------------------------------------
        t.rsi_14,
        t.macd,
        t.macd_signal,
        t.macd_hist,
        t.bollinger_pctb_20,
        t.bollinger_width_20,
        t.stoch_k_14,

        -- ---------------------------------------------------------------
        -- Point-in-time provenance of the fundamental block. Renamed from
        -- available_from so the leakage test reads as what it asserts:
        -- fundamental_available_from must never exceed the bar date.
        -- ---------------------------------------------------------------
        f.period_type,
        f.fiscal_period_end,
        f.available_from                                                  as fundamental_available_from,
        f.days_since_available,

        -- ---------------------------------------------------------------
        -- Headline fundamentals, as reported. Kept raw for the screener and
        -- for anyone rebuilding a ratio by hand.
        -- ---------------------------------------------------------------
        f.revenue,
        f.net_income,
        f.eps_basic,
        f.eps_diluted,
        f.eps_ttm,
        f.revenue_ttm,
        f.net_income_ttm,
        f.operating_cash_flow_ttm,
        f.free_cash_flow_ttm,
        f.ebitda_ttm,
        f.total_assets,
        f.total_equity,
        f.total_debt,
        f.long_term_debt,
        f.cash_and_equivalents,
        f.shares_outstanding,

        -- ---------------------------------------------------------------
        -- Valuation. Quarterly fundamentals over a daily price, so these move
        -- every session even though the fundamental leg changes four times a
        -- year.
        -- ---------------------------------------------------------------
        f.market_cap,
        f.price_to_earnings,
        f.earnings_yield,
        f.price_to_sales,
        f.price_to_book,
        f.fcf_yield,
        f.enterprise_value,
        f.ev_to_ebitda,
        f.debt_to_market_cap,

        -- ---------------------------------------------------------------
        -- Profitability.
        -- ---------------------------------------------------------------
        f.gross_margin,
        f.operating_margin,
        f.net_margin,
        f.roe,
        f.roa,
        f.roic,

        -- ---------------------------------------------------------------
        -- Growth.
        -- ---------------------------------------------------------------
        f.revenue_growth_qoq,
        f.revenue_growth_yoy,
        f.eps_growth_qoq,
        f.eps_growth_yoy,
        f.net_income_growth_yoy,
        f.ocf_growth_yoy,
        f.shares_change_growth_yoy,

        -- ---------------------------------------------------------------
        -- Leverage and balance-sheet health.
        -- ---------------------------------------------------------------
        f.debt_to_equity,
        f.current_ratio,
        f.interest_coverage,
        f.net_debt_to_ebitda,

        -- ---------------------------------------------------------------
        -- Quality.
        -- ---------------------------------------------------------------
        f.accruals,
        f.ocf_to_net_income,
        f.asset_turnover,
        f.capex_to_revenue,

        -- ---------------------------------------------------------------
        -- Regime. What kind of tape the day was, which is what lets a model
        -- separate a stock-specific move from a beta move.
        -- ---------------------------------------------------------------
        m.market_ret_1d,
        m.market_ret_21d,
        m.market_ret_63d,
        m.market_vol_21d,
        m.market_vol_63d,
        m.market_breadth,
        m.market_xs_dispersion,

        -- ---------------------------------------------------------------
        -- Calendar.
        -- ---------------------------------------------------------------
        cal.day_of_week,
        cal.month_of_year,
        cal.is_month_end,
        cal.is_quarter_end

    from {{ ref('int_technicals_daily') }} t

    {#
        INNER on the fundamentals side: int_features_daily is built from the
        same bar table, so it carries exactly the same (symbol, date) grain and
        the join cannot drop or duplicate a row. It is inner rather than left
        so that a future divergence between the two models fails the
        unique_combination_of_columns and row-count expectations loudly instead
        of silently emitting null feature blocks.
    #}
    inner join {{ ref('int_features_daily') }} f
        on  f.symbol = t.symbol
        and f.date   = t.date

    inner join {{ ref('stg_companies') }} c
        on c.symbol = t.symbol

    left join {{ ref('int_market_daily') }} m
        on m.date = t.date

    left join calendar cal
        on cal.date = t.date

),

xs_bounds as (

    {#
        Winsorization bounds, one pair per (date, feature).

        percentile_cont takes an array of fractions and returns an array, so
        the 1st and 99th percentiles cost ONE sort of the date group rather
        than two. With ~500 symbols per date that matters: the alternative is
        two sorts per feature per date.

        percentile_cont is defined on double precision, so every feature is
        cast. The raw numeric columns in `base` are untouched - only the
        derived _z and _vs_sector columns come out as double.
    #}
    select
        date,
        {% for feature in xs_features %}
        percentile_cont(array[0.01, 0.99]) within group (
            order by {{ feature }}::double precision
        ) as {{ feature }}_bounds{{ "," if not loop.last }}
        {% endfor %}
    from base
    group by date

),

sector_median as (

    {#
        The _vs_sector denominator. Median, not mean: sector cross-sections are
        small (a handful of names in some GICS sectors) and one outlier would
        move a mean enough to flip the sign of every peer.
    #}
    select
        date,
        sector,
        {% for feature in xs_features %}
        percentile_cont(0.5) within group (
            order by {{ feature }}::double precision
        ) as {{ feature }}_med{{ "," if not loop.last }}
        {% endfor %}
    from base
    group by date, sector

),

final as (

    select
        b.*,

        {% for feature in xs_features %}
        {#
            Winsorized z-score. The case guard is load-bearing: greatest() and
            least() IGNORE nulls in Postgres rather than propagating them, so
            a null feature clamped without the guard would come back as the
            1st-percentile value and enter the cross-section as a real
            observation at the bottom of the distribution.

            nullif on the standard deviation covers a date where every symbol
            shares one value - a constant cross-section has no z-score, and the
            honest answer is null rather than a division by zero.
        #}
        (
            case
                when b.{{ feature }} is null then null
                else least(
                         greatest(b.{{ feature }}::double precision,
                                  x.{{ feature }}_bounds[1]),
                         x.{{ feature }}_bounds[2])
            end
            - avg(
                case
                    when b.{{ feature }} is null then null
                    else least(
                             greatest(b.{{ feature }}::double precision,
                                      x.{{ feature }}_bounds[1]),
                             x.{{ feature }}_bounds[2])
                end
              ) over w_date
        )
        / nullif(
            stddev_samp(
                case
                    when b.{{ feature }} is null then null
                    else least(
                             greatest(b.{{ feature }}::double precision,
                                      x.{{ feature }}_bounds[1]),
                             x.{{ feature }}_bounds[2])
                end
            ) over w_date, 0)                                              as {{ feature }}_z,

        {#
            Decile within the date, 1 = lowest.

            ntile() is deliberately NOT used. It buckets every row in the
            partition including the nulls, so a feature that is 30% null would
            see its real values squeezed into deciles 1 to 7. Ranking against
            count(feature) - which counts non-nulls only - keeps the ten
            buckets spanning the observed values, and the case guard keeps the
            null rows out of the output entirely.
        #}
        case
            when b.{{ feature }} is null then null
            else least(10,
                     floor(
                         (rank() over (partition by b.date order by b.{{ feature }}) - 1)
                         * 10.0 / nullif(count(b.{{ feature }}) over w_date, 0)
                     )::int + 1)
        end                                                                as {{ feature }}_decile,

        {#
            Sector-relative level, on the RAW value rather than the winsorized
            one: this is a difference against a median, and a median is already
            outlier-proof.
        #}
        b.{{ feature }}::double precision
            - s.{{ feature }}_med                                          as {{ feature }}_vs_sector{{ "," if not loop.last }}
        {% endfor %}

    from base b
    inner join xs_bounds x
        on x.date = b.date
    inner join sector_median s
        on  s.date   = b.date
        and s.sector = b.sector

    window w_date as (partition by b.date)

)

select * from final
