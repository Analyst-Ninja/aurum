{#
    Route models to the literal schema named in their config.

    By default dbt concatenates the profile schema with the custom schema on
    the model, so a model configured as `bronze` under the `aurum_dwh` profile
    (schema: bronze) would land in `bronze_bronze`, and silver models in
    `bronze_silver`. The medallion needs three plain schemas (bronze, silver,
    gold), so return the custom name verbatim when one is set.

    Models with no custom schema fall back to the target schema of the profile.

    NOTE: keep this comment plain ASCII, with no apostrophes. Editors lex a
    .sql file as SQL, where {# #} is not a comment: an apostrophe opens a
    string literal that never closes, and an em dash reads as an invalid
    character. Both light up the file even though dbt strips this block
    before any SQL parser sees it.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}

    {%- set default_schema = target.schema -%}
    {%- if custom_schema_name is none -%}

        {{ default_schema }}

    {%- else -%}

        {{ custom_schema_name | trim }}

    {%- endif -%}

{%- endmacro %}
