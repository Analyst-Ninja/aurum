{#
    THE SINGLE MOST IMPORTANT TEST IN THE PROJECT.

    Asserts at the GOLD boundary what assert_no_fundamental_lookahead.sql
    asserts inside silver: no row may carry a fundamental the market could not
    have seen on that date. The duplication is deliberate. Silver proves the
    as-of join is correct; this proves nothing downstream of it - a widened
    select, a re-ordered join, a new model wedged in between - reintroduced the
    leak on the way to the table that training and inference actually read.

    Three assertions:

      1. fundamental_available_from > date - a future filing was attached
         outright. This is the failure that makes a backtest look brilliant.
      2. days_since_available < 0 - the staleness column disagrees with the
         join predicate, meaning one was edited without the other.
      3. fiscal_period_end > date - a period that had not finished yet. Weaker
         than 1 and strictly implied by it while the lag vars stay positive,
         but it fails loudly if the lag is ever set to zero.

    Rows with no fundamental attached - a symbol early history, before its
    first filing was visible - are not failures. Every predicate is null-safe,
    so those rows are excluded automatically.
#}

{{ config(tags=['leakage']) }}

select
    symbol,
    date,
    period_type,
    fiscal_period_end,
    fundamental_available_from,
    days_since_available

from {{ ref('mart_features') }}

where
       fundamental_available_from > date
    or days_since_available < 0
    or fiscal_period_end > date
