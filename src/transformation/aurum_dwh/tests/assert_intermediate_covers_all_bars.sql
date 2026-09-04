{#
    Gap detector for the incremental warm-up guard.

    int_technicals_daily and int_features_daily read window_lookback_days of
    history but only write the window_rewrite_days tail. That asymmetry is what
    keeps an incremental run bit-identical to a full refresh - but it also means
    the pipeline may not lapse longer than the rewrite tail. If it does, the
    dates between the last run and the start of the new tail are read as
    warm-up, never emitted, and silently missing forever.

    This test asserts every bar in stg_ohlcv_daily reached both models. It is
    the alarm that turns "the pipeline was down for four months" from a hole
    nobody notices into a failed build.

    Reported by date rather than by (symbol, date) so a real gap surfaces as a
    readable handful of rows instead of half a million.
#}

with

expected as (

    select distinct date from {{ ref('stg_ohlcv_daily') }}

),

missing_technicals as (

    select
        e.date,
        'int_technicals_daily' as missing_from
    from expected e
    where not exists (
        select 1 from {{ ref('int_technicals_daily') }} t where t.date = e.date
    )

),

missing_features as (

    select
        e.date,
        'int_features_daily' as missing_from
    from expected e
    where not exists (
        select 1 from {{ ref('int_features_daily') }} f where f.date = e.date
    )

)

select * from missing_technicals
union all
select * from missing_features
