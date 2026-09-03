{#
    Typed, deduplicated mirror of public.cashflow_stmts_yearly. Follows the shape of
    br_income_stmts_quarterly, the bronze reference model, changing only the
    source name and the period column (FY).

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

    select * from {{ source('landing', 'cashflow_stmts_yearly') }}

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
        Landing appends, so one (MD5_HASH, CONCEPT) pair can appear many times
        across runs. Keep the newest write.

        EXECUTION_ID is a YYYYMMDD_HHMMSSssssss stamp, so a plain descending
        string sort is chronological.
    #}
    select
        *,
        row_number() over (
            partition by "MD5_HASH", "CONCEPT"
            order by "EXECUTION_ID" desc, "RUN_DATE" desc
        ) as _row_num

    from source

),

renamed as (

    select
        -- Natural key
        "SYMBOL"                        as symbol,
        "FY"                            as fy,

        --
        "CONCEPT"                       as concept,
        "LABEL"                         as label,
        "DEPTH"::bigint                 as depth,
        "IS_ABSTRACT"::boolean          as is_abstract,
        "IS_TOTAL"::boolean             as is_total,
        "SECTION"                       as section,
        "CONFIDENCE"::numeric           as confidence,
        "VALUE"::numeric                as value,

        -- Ingestion metadata, carried through for lineage and watermarking.
        "RUN_DATE"::date                as run_date,
        "EXECUTION_ID"                  as execution_id,
        "MD5_HASH"                      as md5_hash

    from deduplicated
    where _row_num = 1

)

select * from renamed
