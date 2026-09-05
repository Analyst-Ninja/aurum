{#
    All six bronze statement mirrors stacked into one long table.
    Grain: (symbol, statement, period_type, fiscal_period_end, concept).

    Bronze keeps income / cashflow / balance_sheet x quarterly / yearly as six
    separate mirrors of six separate landing tables. Every consumer downstream
    wants them as one long fact, so the union happens exactly once, here, and
    the three things that only make sense after the union happen with it:

      1. tag      - statement + period_type become columns, not table names
      2. parse    - the text period label becomes a real fiscal_period_end date
      3. map      - concept_map attaches canonical_name (unmapped stays NULL)
      4. dedupe   - one row per grain, latest ingest wins

    Still LONG, not pivoted. The concept -> canonical_name pivot belongs in the
    next model, where priority-based collision resolution can be tested on its
    own rather than buried in a union of six.
#}

{{
    config(
        materialized='incremental',
        unique_key=['symbol', 'statement', 'period_type', 'fiscal_period_end', 'concept'],
        incremental_strategy='delete+insert'
    )
}}

{#
    (bronze model, statement tag, period_type tag, period label column).
    Quarterly mirrors name the label column `qtr`, yearly ones name it `fy`;
    aliasing it here is the only structural difference between the six.
#}
{% set statement_sources = [
    ('br_income_stmts_quarterly',        'income',        'quarterly', 'qtr'),
    ('br_income_stmts_yearly',           'income',        'annual',    'fy'),
    ('br_cashflow_stmts_quarterly',      'cashflow',      'quarterly', 'qtr'),
    ('br_cashflow_stmts_yearly',         'cashflow',      'annual',    'fy'),
    ('br_balance_sheet_stmts_quarterly', 'balance_sheet', 'quarterly', 'qtr'),
    ('br_balance_sheet_stmts_yearly',    'balance_sheet', 'annual',    'fy')
] %}

with

unioned as (

{% for model_name, statement, period_type, period_column in statement_sources %}

    select
        symbol,
        '{{ statement }}'::text     as statement,
        '{{ period_type }}'::text   as period_type,

        -- Raw label kept verbatim. The parse below is the fragile step; when it
        -- is wrong this column is the only way to see what it was wrong about.
        {{ period_column }}         as period_label,

        concept,
        label,
        depth,
        is_abstract,
        is_total,
        section,
        confidence,
        value,

        run_date,
        execution_id,
        md5_hash

    from {{ ref(model_name) }}

    {% if is_incremental() %}
    {#
        Same run_date lookback the bronze models use. A backfill re-lands old
        periods under a recent run_date, so the window is on ingest time, not
        on fiscal_period_end. delete+insert then replaces every grain the
        window touched, which is what makes the dedupe below hold across runs.
    #}
    where run_date >= (
        select coalesce(max(run_date), '1900-01-01'::date)
             - interval '{{ var("window_lookback_days") }} days'
        from {{ this }}
    )
    {% endif %}

    {% if not loop.last %}union all{% endif %}

{% endfor %}

),

parsed as (

    {#
        Period labels arrive as 'Q1 2024' and 'FY 2016'. The space in the
        annual form is real - it is 'FY 2016', not 'FY2016' - but the regexes
        make it optional so a change in the ingestion label format does not
        silently start producing NULLs on one of the two shapes.

        Anything that matches neither shape yields a NULL fiscal_period_end and
        is caught by tests/assert_no_unparsed_fiscal_periods.sql, which fails
        the build rather than letting an unparsed period become a silent gap.

        CAVEAT: this maps to CALENDAR quarter ends. A company whose fiscal year
        ends in, say, June has its 'Q1 2024' land on 2024-03-31 here rather than
        on its true fiscal period end. EDGAR ingestion does not currently carry
        period_end, so this is the best available approximation; it is accurate
        for the ~75% of the index on a December fiscal year end and shifts the
        rest by up to a quarter. Replace with the real period_end once ingested.
    #}
    select
        *,
        substring(period_label from '([0-9]{4})\s*$')::int   as fiscal_year,
        case
            when period_type = 'quarterly'
                then substring(period_label from '^\s*Q([1-4])')::int
        end                                                  as fiscal_quarter
    from unioned

),

dated as (

    select
        *,
        case
            {#
                Last day of the quarter: first day of the quarter closing
                month, plus a month, minus a day. Avoids hardcoding 31/30.
            #}
            when period_type = 'quarterly' and fiscal_year is not null and fiscal_quarter is not null
                then (make_date(fiscal_year, fiscal_quarter * 3, 1)
                      + interval '1 month' - interval '1 day')::date

            when period_type = 'annual' and fiscal_year is not null
                then make_date(fiscal_year, 12, 31)
        end as fiscal_period_end
    from parsed

),

mapped as (

    {#
        LEFT join, deliberately. An unmapped concept keeps its row with
        canonical_name NULL so seed coverage stays measurable - a count of
        NULLs here is the coverage gap. An inner join would make bad coverage
        look like clean data.

        Joined on (concept, statement), not concept alone. concept is unique
        within the seed, so the statement predicate adds no rows; what it does
        is decontaminate cross-statement leakage - a balance sheet concept that
        turns up inside an income statement filing does not silently acquire a
        canonical name it has no business having.
    #}
    select
        d.*,
        cm.canonical_name,

        {#
            sign and priority ride along from the same join, deliberately. The
            pivot model immediately downstream needs both - sign to normalise
            concepts reported with inverted polarity, priority to resolve two
            concepts competing for one canonical_name - and re-joining the seed
            there to fetch them would be pure waste. Keep them.
        #}
        cm.sign             as canonical_sign,
        cm.priority         as concept_priority

    from dated d
    left join {{ ref('concept_map') }} cm
        on  d.concept   = cm.concept
        and d.statement = cm.statement

),

deduplicated as (

    {#
        The specs amendment-supersedes rule, approximated. The real rule keeps
        the latest filed_date per (cik, metric, period_end) so a 10-K/A beats
        the 10-K it amends. EDGAR ingestion does not carry filed_date yet, so
        this stands in "latest ingest wins": within one grain, the row from the
        most recent feed run is the surviving one.

        That holds as long as amendments are ingested after the filings they
        amend, which is true for a forward-running feed and false for an
        out-of-order backfill. Swap the ordering key for filed_date when it
        lands - the partition is already the right one.

        EXECUTION_ID is a YYYYMMDD_HHMMSSssssss stamp, so a descending string
        sort is chronological. md5_hash breaks ties so the pick is stable
        across runs rather than dependent on scan order.
    #}
    select
        *,
        row_number() over (
            partition by symbol, statement, period_type, fiscal_period_end, concept
            order by execution_id desc, run_date desc, md5_hash desc
        ) as _row_num

    from mapped

),

final as (

    select
        -- Grain.
        symbol,
        statement,
        period_type,
        fiscal_period_end,
        concept,

        -- Period, both parsed and as-landed.
        fiscal_year,
        fiscal_quarter,
        period_label,

        -- Concept mapping. NULL canonical_name = concept not in the seed.
        canonical_name,
        canonical_sign,
        concept_priority,

        -- The fact and its XBRL presentation context.
        value,
        label,
        depth,
        is_abstract,
        is_total,
        section,
        confidence,

        -- Ingestion metadata, carried through for lineage and watermarking.
        run_date,
        execution_id,
        md5_hash

    from deduplicated
    where _row_num = 1

)

select * from final
