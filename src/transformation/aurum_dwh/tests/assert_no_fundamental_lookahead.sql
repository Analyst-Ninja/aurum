{#
    The single most important test in the project.

    int_features_daily attaches a quarterly fundamental to a daily bar. If the
    join ever matches a fundamental whose visibility date is in the future
    relative to the bar, the model is being trained on information nobody had -
    and a backtest built on it will look excellent and lose money live.

    Two assertions, because there are two ways to get this wrong:

      1. available_from > date  - the join matched a future filing outright.
      2. days_since_available < 0 - the derived staleness column disagrees with
         the join predicate, which would mean one of them was edited without
         the other.

    Rows with no fundamental attached (early history, before a symbol's first
    filing) are not failures: available_from is NULL there and both predicates
    are NULL-safe, so those rows are excluded automatically.
#}

select
    symbol,
    date,
    period_type,
    fiscal_period_end,
    available_from,
    days_since_available

from {{ ref('int_features_daily') }}

where
       available_from > date
    or days_since_available < 0
