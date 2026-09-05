{#
    The sector dimension. One row per symbol, from the S&P 500 constituent
    seed scraped off Wikipedia.

    Materialized as a table, not incremental: 503 rows with no ingestion
    metadata to window on. It is a seed cleanup, so it rebuilds in full.

    Symbols are already in Yahoo punctuation (BRK-B, BF-B, not BRK.B), which
    is the same form the OHLCV landing tables carry, so joins to stg_ohlcv_daily
    need no translation. Do not "fix" the hyphens.
#}

{{
    config(
        materialized='table'
    )
}}

with

source as (

    select * from {{ ref('company_meta') }}

),

cleaned as (

    select
        {#
            Trimmed and uppercased defensively. The scrape is clean today (zero
            untrimmed symbols, 503 rows, 503 distinct) but it is a scrape, and
            a stray space here becomes a silently empty join downstream.
        #}
        upper(btrim(symbol))                as symbol,

        btrim(company_name)                 as company_name,
        btrim(sector)                       as sector,
        btrim(industry)                     as industry,
        btrim(headquarter_locations)        as headquarter_location,

        -- Date the company entered the index. Useful as a survivorship guard.
        date_added::date                    as date_added,

        {#
            CIK is the EDGAR key. Kept as a number for joins, and also rendered
            as the zero-padded 10-character string EDGAR own URLs and JSON
            payloads use, so downstream code does not re-derive the padding.
        #}
        cik::bigint                         as cik,
        lpad(cik::text, 10, '0')            as cik_padded,

        {#
            `founded` is free text. 39 of 503 rows carry annotations like
            "2013 (1888)" - the year the current entity was formed, with the
            year of the predecessor it descends from in parentheses. Take the
            leading year and keep the raw string beside it.
        #}
        substring(btrim(founded) from '^([0-9]{4})')::int as founded_year,
        btrim(founded)                                    as founded_raw

    from source

)

select * from cleaned
