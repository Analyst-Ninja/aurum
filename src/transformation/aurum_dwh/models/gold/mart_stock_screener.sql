{#
    The analyst-facing mart. Grain: one row per symbol.

    The query target for the future FastMCP server, which turns a natural
    language question into SQL against this table and nothing else. That is why
    it is deliberately narrow and flat: every column is a headline number a
    human would ask for by name, with no z-scores, no deciles and no window
    functions to explain.

    Latest bar PER SYMBOL, not the latest bar in the warehouse. A symbol whose
    history stops early - delisted, acquired, renamed upstream - still gets its
    last known row rather than vanishing, and price_date says plainly how stale
    that row is.

    Column contract: docs/warehouse/data-dictionary.md, screener table. Two documented
    deviations from that table, both because it describes the target v2 system
    rather than what is built:

      1. ma_30d / ma_90d / vol_30d / sharpe_30d do not exist. Silver builds
         moving averages at 10/20/50/200 and risk windows at 21/63/252, which
         are the conventional trading-day equivalents. ma_50, ma_200, vol_21d
         and sharpe_21d are carried instead, under their real names.
      2. sentiment_7d and news_count_7d are absent. The news domain is not
         ingested - there is no sentiment anywhere in the warehouse, and a
         column of nulls would suggest otherwise.
#}

{{
    config(
        materialized='table'
    )
}}

with

latest_bar as (

    {#
        distinct on is the Postgres idiom for argmax: sort each symbol by date
        descending and keep the first row. One row per symbol by construction,
        which is what the acceptance criterion asks for - no group-by plus
        self-join, and no chance of a tie fanning out.
    #}
    select distinct on (symbol) *
    from {{ ref('mart_features') }}
    order by symbol, date desc

),

final as (

    select
        -- Identity.
        b.symbol,
        c.company_name,
        b.sector,
        b.industry,

        -- Latest market state.
        b.date                                                            as price_date,
        b.close_raw                                                       as latest_price,
        b.adj_close,
        b.adv_21d,

        -- Which filing these fundamentals came from, and how stale it is.
        b.fiscal_period_end                                               as period_end,
        b.period_type,
        b.fundamental_available_from,
        b.days_since_available,

        -- Headline fundamentals, raw dollars as reported.
        b.revenue,
        b.net_income,
        b.eps_basic,
        b.eps_ttm,
        b.long_term_debt,
        b.total_debt,
        b.shares_outstanding,

        -- Valuation.
        b.market_cap,
        b.enterprise_value,
        b.price_to_earnings,
        b.price_to_sales,
        b.price_to_book,
        b.ev_to_ebitda,
        b.fcf_yield,
        b.debt_to_market_cap,

        -- Profitability and growth.
        b.gross_margin,
        b.operating_margin,
        b.net_margin,
        b.roe,
        b.roic,
        b.revenue_growth_qoq,
        b.revenue_growth_yoy,
        b.eps_growth_yoy,

        -- Balance-sheet health.
        b.debt_to_equity,
        b.current_ratio,
        b.net_debt_to_ebitda,

        -- Trend and risk indicators.
        b.ret_21d,
        b.ret_252d,
        b.ma_50,
        b.ma_200,
        b.price_to_ma_50,
        b.price_to_ma_200,
        b.dist_from_52w_high,
        b.vol_21d,
        b.vol_252d,
        b.sharpe_21d,
        b.beta_252d,
        b.max_drawdown_252d,
        b.rsi_14,

        {#
            Two cross-sectional anchors, carried so the MCP server can answer
            questions of the form cheap relative to its sector without
            recomputing a percentile at query time.
        #}
        b.earnings_yield_decile,
        b.net_margin_vs_sector

    from latest_bar b
    inner join {{ ref('stg_companies') }} c
        on c.symbol = b.symbol

)

select * from final
