{#
    Targets must be NULL at the right edge of the panel, not zero.

    fwd_ret_5d is ln(close 5 bars ahead / close today). On the last five traded
    dates in the warehouse those five bars do not exist yet, so lead() returns
    null and the target must stay null all the way into the table.

    Why this is worth a dedicated test: a zero forward return is a REAL,
    perfectly plausible observation. If a coalesce, a fillna in a loader or a
    join against a padded date spine ever turns the edge into zeros, nothing
    looks wrong - the column is fully populated, the row count is right, and
    the model quietly learns that the most recent week is always flat. Those
    are precisely the rows a fresh backtest scores.

    Symmetric check on fwd_ret_21d over the last 21 dates, and on the two
    derived 5-day targets which must inherit the null rather than defaulting.

    Note this asserts the edge is null, NOT that every null is at the edge:
    a delisted symbol runs out of forward bars early and is legitimately null
    long before the panel does.
#}

{{ config(tags=['leakage']) }}

with

trading_days as (

    {#
        Rank distinct dates from the end. dense_rank, not row_number: the panel
        has ~500 symbols per date, and the rank must count DATES.
    #}
    select
        date,
        dense_rank() over (order by date desc) as days_from_end
    from (
        select distinct date from {{ ref('mart_training_set') }}
    ) d

),

edge_rows as (

    select
        t.symbol,
        t.date,
        d.days_from_end,
        t.fwd_ret_5d,
        t.fwd_ret_21d,
        t.fwd_ret_5d_excess,
        t.fwd_ret_5d_xs_decile,
        t.label_up_5d
    from {{ ref('mart_training_set') }} t
    inner join trading_days d
        on d.date = t.date

)

select *
from edge_rows
where
    (
        days_from_end <= 5
        and (
               fwd_ret_5d           is not null
            or fwd_ret_5d_excess    is not null
            or fwd_ret_5d_xs_decile is not null
            or label_up_5d          is not null
        )
    )
    or (days_from_end <= 21 and fwd_ret_21d is not null)
