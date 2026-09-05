{#
    REFERENCE MODEL for silver staging. One row per (symbol, date).

    Bronze mirrors landing and preserves bad ticks by design. Staging is where
    they are removed and where the split/dividend adjustment is applied, so
    everything downstream reads a single continuous price series.

    Staging does three things:
      1. filter  - drop rows whose OHLC/volume cannot be trusted
      2. adjust  - scale prices (and share counts) by adj_close / close
      3. derive  - only what is a property of the bar itself, e.g. dollar_volume

    Staging does NOT compute returns, rolling windows, or anything that looks
    across rows. Those belong in the silver feature models, where the lookback
    is an explicit part of the model rather than a side effect of the
    incremental filter.
#}

{{
    config(
        materialized='incremental',
        unique_key='md5_hash',
        incremental_strategy='delete+insert'
    )
}}

with

base as (

    select * from {{ ref('br_ohlcv_1d') }}

    where
        {#
            Bad ticks. A zero or null volume bar is a non-trading artifact
            (holiday padding, halted name); a non-positive close is a parse
            error. adj_close is guarded too: it is the numerator of the
            adjustment factor, so a null there would silently null out every
            adjusted price on the row instead of dropping it.
        #}
            volume is not null
        and volume <> 0
        and close > 0
        and adj_close > 0
        and open > 0
        and high > 0
        and low > 0

        -- Internal consistency: the bar's high/low must actually bound it.
        and high >= low
        and high >= close
        and high >= open
        and low <= close
        and low <= open

    {% if is_incremental() %}
    {#
        Filter on run_date, not date, to match the bronze lookback. A backfill
        re-lands old bar dates under a recent run_date; filtering on `date`
        would leave those corrections outside the window and never pick them
        up. delete+insert on md5_hash then replaces exactly the re-read rows.
    #}
    and run_date >= (
        select coalesce(max(run_date), '1900-01-01'::date)
             - interval '{{ var("window_lookback_days") }} days'
        from {{ this }}
    )
    {% endif %}

),

with_adj_factor as (

    {#
        Yahoo gives adj_close directly but leaves the other three legs raw.
        adj_close / close is that bars cumulative split+dividend factor, and
        applying it to open/high/low puts the whole bar on the adjusted basis.
        Division is safe: base guarantees close > 0.
    #}
    select
        *,
        (adj_close / close) as adj_factor
    from base

),

adjusted_data as (

    select
        symbol,
        date,

        (open * adj_factor)     as adj_open,
        (high * adj_factor)     as adj_high,
        (low * adj_factor)      as adj_low,

        {#
            Carried through as landed, not recomputed as close * adj_factor:
            that expression is adj_close by construction and only adds a
            round-trip through the division.
        #}
        adj_close,
        adj_factor,

        {#
            Share count on the same adjusted basis as the prices. A 4:1 split
            quadruples raw volume overnight, which would put a false step into
            every rolling-volume feature built on top of this.
        #}
        (volume / adj_factor)   as adj_volume,
        volume                  as raw_volume,

        {#
            Deliberately raw close x raw volume. Notional traded is already
            split-invariant - the split scales price down and share count up by
            the same factor - so adjusting either leg would double-count. This
            is the column var('min_adv_usd') is applied against.
        #}
        (volume * close)        as dollar_volume,

        stock_splits,
        dividends,

        -- Ingestion metadata, carried through for lineage and watermarking.
        run_date,
        execution_id,

        {#
            Inherited from bronze, whose ingestion config sets cols_for_pk to
            (SYMBOL, DATE). md5_hash is therefore 1:1 with the (symbol, date)
            grain asserted by unique_combination_of_columns in the schema file,
            which is what makes it a valid unique_key here.
        #}
        md5_hash

    from with_adj_factor

)

select * from adjusted_data
