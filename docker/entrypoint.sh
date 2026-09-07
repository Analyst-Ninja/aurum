#!/bin/sh
# One image, three workloads. The first argument picks which CLI runs; everything after
# it is passed through untouched.
#
#   ingest -c src/ingestion/configs/yahoo/ohlcv_1d.yaml -f False
#   dbt    build --select bronze
#   model  train -c src/modeling/configs/lgbm_xs_excess_5d.yaml
#
# Anything else is exec'd verbatim, so `sh`, `ls` and `--help` still work for debugging.
set -eu

DBT_PROJECT_DIR=/app/src/transformation/aurum_dwh

if [ "$#" -eq 0 ]; then
    exec python -m src.modeling.cli --help
fi

command=$1
shift

case "$command" in
    ingest)
        exec python -m src.ingestion.cli "$@"
        ;;
    dbt)
        # dbt resolves the project from the working directory; DBT_PROFILES_DIR (set in
        # the Dockerfile) points at the env-var-templated profile under docker/dbt/.
        cd "$DBT_PROJECT_DIR"
        exec dbt "$@"
        ;;
    model)
        exec python -m src.modeling.cli "$@"
        ;;
    *)
        exec "$command" "$@"
        ;;
esac
