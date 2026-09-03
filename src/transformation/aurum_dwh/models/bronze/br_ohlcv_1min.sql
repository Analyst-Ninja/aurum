{#
    REFERENCE MODEL for the bronze layer. The other six br_* models follow this
    shape exactly, changing only the source name and the column list.

    Bronze does three things and nothing else:
      1. deduplicate  - landing appends on every run, so rows repeat
      2. type         - landing columns are text/double, whatever pandas wrote
      3. rename       - uppercase quoted identifiers become snake_case

    Bronze does NOT clean. No filtering bad ticks, no adjusting prices, no
    parsing period labels. Those belong in silver staging, where they can be
    tested and reasoned about separately from the mirror.
#}

{{
    config(
        materialized='incremental',
        unique_key='md5_hash',
        incremental_strategy='delete+insert'
    )
}}

with

source as (

    select * from {{ source('landing', 'ohlcv_1min') }}

    {% if is_incremental() %}
    {#
        Re-read a window of history rather than only new rows. RUN_DATE is
        stored as text, so it is cast before comparison. delete+insert then
        replaces that whole window, which keeps the model idempotent when a
        backfill re-lands old run dates.
    #}
    where "RUN_DATE"::date >= (
        select coalesce(max(run_date), '1900-01-01'::date)
             - interval '{{ var("window_lookback_days") }} days'
        from {{ this }}
    )
    {% endif %}

),

deduplicated as (

    {#
        Landing appends, so one MD5_HASH can appear many times. Observed in
        this table: 1503 rows are repeats, and within a repeat group the
        earlier row can carry a NULL close that a later run filled in. Keep
        the newest write.

        EXECUTION_ID is a YYYYMMDD_HHMMSSssssss stamp, so a plain descending
        string sort is chronological.
    #}
    select
        *,
        row_number() over (
            partition by "MD5_HASH"
            order by "EXECUTION_ID" desc, "RUN_DATE" desc
        ) as _row_num

    from source

),

renamed as (

    select
        -- Natural key
        "SYMBOL"                        as symbol,
        "DATE"::timestamp               as datetime,

        -- Prices. Landing stores these as double precision; numeric avoids
        -- float drift once returns and ratios are computed on top.
        "OPEN"::numeric                 as open,
        "HIGH"::numeric                 as high,
        "LOW"::numeric                  as low,
        "CLOSE"::numeric                as close,

        -- Quoted because the landing columns really do contain spaces.
        "ADJ CLOSE"::numeric            as adj_close,
        "STOCK SPLITS"::numeric         as stock_splits,
        "DIVIDENDS"::numeric            as dividends,

        -- Volume arrives as double precision despite being a share count.
        "VOLUME"::bigint                as volume,

        -- Ingestion metadata, carried through for lineage and watermarking.
        "RUN_DATE"::date                as run_date,
        "EXECUTION_ID"                  as execution_id,
        "MD5_HASH"                      as md5_hash

    from deduplicated
    where _row_num = 1

)

select * from renamed
