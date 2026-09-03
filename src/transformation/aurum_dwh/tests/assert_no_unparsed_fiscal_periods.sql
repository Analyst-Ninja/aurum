{#
    The period-label parser in stg_financials_long is the most fragile piece of
    the silver layer: it turns vendor text ('Q1 2024', 'FY 2016') into a real
    date, and every fundamentals model downstream is keyed on that date.

    A parse failure does not error - it produces a NULL fiscal_period_end, which
    collapses the model grain and quietly drops the row from any date-bounded
    join. This test makes that failure loud.

    Fails if ANY row has an unparsed period. Returns the offending labels with
    a count so the failure message names the format that broke, rather than
    just asserting that something did.
#}

select
    statement,
    period_type,
    period_label,
    count(*) as n_rows

from {{ ref('stg_financials_long') }}

where fiscal_period_end is null
   or fiscal_year is null
   -- A quarterly row without a quarter parsed out is equally unparsed, even
   -- though the year alone would have yielded a non-null date.
   or (period_type = 'quarterly' and fiscal_quarter is null)

group by 1, 2, 3
order by n_rows desc
