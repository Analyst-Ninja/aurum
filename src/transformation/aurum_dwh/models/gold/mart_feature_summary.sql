{#
    The training view after feature selection. Grain: (symbol, date).

    mart_training_set narrowed to the columns named in seeds/selected_features.
    That seed is written by the SHAP loop in src/modeling; regenerating this
    model after a run is:

        uv run --group dbt dbt seed
        uv run --group dbt dbt run --select mart_feature_summary

    and that is the entire feature-selection loop from spec section 3.7.

    THIS MODEL MUST COMPILE BEFORE ANY MODEL HAS EVER BEEN TRAINED. The seed
    ships with a single placeholder row, and nothing here may assume a real
    SHAP run exists. Three guards make that true:

      1. the seed relation is looked up with load_relation, so a project that
         has not been seeded yet compiles instead of failing on a missing table
      2. requested names are intersected with the real columns of
         mart_training_set, so a stale seed naming a feature that was renamed
         or dropped is skipped rather than raising a column-does-not-exist
      3. if that intersection is empty, the model falls back to the full
         training set - an unselected panel, never an empty one

    Keys, targets and fold_id are always carried whether the seed names them or
    not. A feature list without its label is not trainable, and every consumer
    of this model expects to find them.
#}

{{
    config(
        materialized='table'
    )
}}

{#
    Both refs below are reached inside conditional blocks, which dbt cannot see
    when it builds the DAG. These hints declare the edges explicitly - without
    them dbt raises "unable to infer all dependencies" and the model could be
    scheduled before the seed and the training set exist.
#}
-- depends_on: {{ ref('selected_features') }}
-- depends_on: {{ ref('mart_training_set') }}

{#
    Always-present columns. Kept out of the feature list so a seed that happens
    to name one of them cannot emit it twice.
#}
{% set key_columns = [
    'symbol',
    'date',
    'sector',
    'fold_id',
    'fwd_ret_5d',
    'fwd_ret_21d',
    'fwd_ret_5d_excess',
    'fwd_ret_5d_xs_decile',
    'label_up_5d'
] %}

{% set selected_columns = [] %}

{% if execute %}

    {% set seed_relation     = load_relation(ref('selected_features')) %}
    {% set training_relation = load_relation(ref('mart_training_set')) %}

    {% if seed_relation is not none and training_relation is not none %}

        {#
            selected is a boolean in Postgres once dbt has typed the seed, but
            it lands as text if the seed is ever loaded with explicit
            column_types. Casting to text and matching the three truthy
            renderings keeps this working either way.
        #}
        {% set seed_query %}
            select feature_name
            from {{ seed_relation }}
            where lower(selected::text) in ('true', 't', '1')
            order by rank
        {% endset %}

        {% set requested = run_query(seed_query).columns[0].values() %}

        {% set available = [] %}
        {% for column in adapter.get_columns_in_relation(training_relation) %}
            {% do available.append(column.name | lower) %}
        {% endfor %}

        {% for feature in requested %}
            {% if feature is not none %}
                {% set feature_name = feature | trim | lower %}
                {% if feature_name in available
                      and feature_name not in key_columns
                      and feature_name not in selected_columns %}
                    {% do selected_columns.append(feature_name) %}
                {% endif %}
            {% endif %}
        {% endfor %}

    {% endif %}

{% endif %}

{% if selected_columns | length == 0 %}

{#
    Fallback path: nothing selected yet, or the seed names only columns this
    warehouse does not have. Emit the whole training set so the model is still
    a valid, trainable relation - the selection loop narrows it later.
#}
select * from {{ ref('mart_training_set') }}

{% else %}

select
    {% for column in key_columns %}
    {{ column }},
    {% endfor %}

    {% for column in selected_columns %}
    {{ column }}{{ "," if not loop.last }}
    {% endfor %}

from {{ ref('mart_training_set') }}

{% endif %}
