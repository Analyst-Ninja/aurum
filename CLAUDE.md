# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

AURUM is mid-Phase-0. The **design** (`docs/architecture/TECHNICAL_SPEC.md`, spec v2.0) describes the full target system — Kafka backbone, Snowflake medallion, ML + MCP server. Most of that does not exist yet.

What actually exists and runs today:

- `src/ingestion/` — a working config-driven ingestion framework (Yahoo OHLCV + EDGAR financial statements → **Postgres directly**, no Kafka in the code path yet).
- `src/transformation/aurum_dwh/` — a dbt project pointed at **Postgres** (not Snowflake), with the full medallion **bronze**, **silver** and **gold** layers built and tested: 8 `br_*` mirrors, 3 `stg_*` models, 5 `int_*` feature models, 4 `mart_*` marts, 3 seeds, 237 tests (2 warn on documented real-data outliers, 0 error). The `dbt init` example models are gone. `gold.mart_features` holds ~2.9M rows across 503 symbols, 2000 → today. `docs/warehouse/dwh-medallion.md` documents it as built.
- `src/feed/`, `src/inference/`, `src/mcp/`, `src/modeling/`, `airflow/`, `infra/` — empty `__init__.py` placeholders. `main.py` is empty.
- `tests/` exists but holds no tests; the pytest step in CI is commented out.

`README.md` ("Current state") and `docs/ingestion/datasource-framework.md` describe the code as it is; `docs/architecture/TECHNICAL_SPEC.md` describes the target. `repo_structure.md` is an aspirational tree and does not match `src/`.

## Commands

Package/venv managed with **uv** (Python 3.12).

```bash
uv sync --locked --no-build          # install deps from uv.lock
uv run ruff check src/ main.py       # lint (ruff) — the CI gate
uv run ruff check --fix src/ main.py # lint + autofix
uv run pytest tests/ -v              # tests (no tests written yet)
uv run pytest tests/path::test_name  # run a single test

# run an ingestion feed (config drives everything)
uv run python -m src.ingestion.cli -c src/ingestion/configs/yahoo/ohlcv_1d.yaml            # full load
uv run python -m src.ingestion.cli -c src/ingestion/configs/yahoo/ohlcv_1d.yaml -f False   # incremental
uv run python -m src.ingestion.cli -c src/ingestion/configs/edgar/income_statements_quarterly.yaml -d 2026-01-01
#   -c/--config  path to feed YAML     -d/--run_date  default today
#   -f/--full_load  True|False, default True — False resumes from the watermarks

# dbt (project dir must be the dbt project root)
# dbt lives in the `dbt` dependency group, NOT the default sync — `--group dbt` is required on every call
cd src/transformation/aurum_dwh
uv run --group dbt dbt debug                       # profile aurum_dwh reaches Postgres aurum
uv run --group dbt dbt deps                        # dbt_utils + dbt_expectations
uv run --group dbt dbt seed                        # company_meta, concept_map, selected_features
uv run --group dbt dbt build                       # all models + all 237 tests
uv run --group dbt dbt build --select bronze       # one layer at a time (bronze|silver|gold)
uv run --group dbt dbt run --select mart_features+ # a model and everything downstream
uv run --group dbt dbt test --select mart_features # tests for one model
```

Add dependencies with `uv add <pkg>` — CI runs `--locked` and fails on `pyproject.toml`/`uv.lock` drift.

**Dependency groups.** `dbt-postgres` sits in a `dbt` group rather than `[project].dependencies`, because dbt is a CLI that `src/` never imports. This keeps it out of the default sync: `dbt-core` pulls `dbt-core-experimental-parser`, which publishes an sdist with no wheel and so cannot install under CI's `--no-build`. Anything importable by `src/` belongs in `[project].dependencies`; tooling belongs in a group.

## Ingestion framework (`src/ingestion/`)

This is the only substantial subsystem. One YAML config fully specifies a run; nothing is wired in Python.

```
configs/*.yaml ──▶ runner.run_feed() ──▶ factory.create_feed()  ──▶ Feed.run(run_date, full_load)
                                          │                            │
                                          └ create_datasource() ×2      ├ output_ds.get_watermarks()   (incremental only)
                                            (input + output)            ├ input_ds.read_data(run_date, watermarks)
                                                                        ├ Feed.process(df)      ← the only per-feed code
                                                                        ├ _add_write_metadata() ← RUN_DATE, EXECUTION_ID, MD5_HASH
                                                                        └ output_ds.write_data(run_date, df)
```

- **Registry, not imports** — `@register_datasource("yahoo_ohlcv")` / `@register_feed("ohlcv_1d")` (`factory/registory.py`) populate dicts the factory looks up by the config's `type`. Decorators only fire when the module is imported, so **every new feed/datasource module must be imported in `src/ingestion/runner.py`** or the factory raises "type not supported". That file's `F401` unused-import warnings are deliberately ignored in `pyproject.toml`.
- **`BaseDatasource`** (`datasources/base_datasource.py`) — `read_data(run_date, watermarks) -> DataFrame` + `write_data(run_date, df)`. API sources stub out `write_data`; sinks stub out `read_data`. `datasources/api/` = read side, `datasources/storage/` = write side.
- **`BaseFeed`** (`feed/base_feed.py`) — owns the whole run loop, metrics, and error swallowing (`run()` catches everything and returns a metrics dict; it does **not** raise). Subclasses implement only `process()`.
- **Incremental by watermark** — when `full_load` resolves to `False`, the feed calls `output_ds.get_watermarks(group_by, date_column)`, which `SELECT MAX(date) GROUP BY symbol` on the landing table; the datasource then starts each symbol the day after its watermark. A missing table returns `{}` (first run) rather than erroring. Don't add full-refresh paths. `-f/--full_load` is an explicit `True|False` and **defaults to `True`**, so incremental runs need `-f False`; the YAML's `full_load` key is not read.
- **Column convention: uppercase.** Feeds uppercase every column in `process()`; configs, `cols_for_pk`, and watermark columns are all uppercase (`SYMBOL`, `DATE`, `QTR`). Postgres identifiers are quoted, so case matters.
- **Deterministic PK** — `_add_write_metadata` md5s the `cols_for_pk` values into the `primary_key` column. Both keys are required in the output config or the run fails.
- Config secrets are **env var *names***, not values: `username: "AURUM_USERNAME"` is `os.getenv`-ed at connect time from `.env` (`HOST`, `PORT`, `AURUM_USERNAME`, `AURUM_PASSWORD`, `SEC_USER_AGENT`).

Adding a source: new class in `datasources/api/<vendor>/` with `@register_datasource`, new feed in `feed/` with `@register_feed`, new YAML in `configs/<vendor>/`, then import both in `runner.py`.

## Target architecture (spec §5 — not yet built)

```
datasources/apis → producers → Kafka topics → consumers → Postgres (landing)
                                                              │ Airflow incremental load
                                                              ▼
                                             Snowflake RAW → SILVER → GOLD (dbt medallion)
                                                              │
                                       ┌──────────────────────┴──────────────┐
                                       ▼                                     ▼
                            modeling/ (train + SHAP)              mcp/ (FastMCP NL→SQL)
                                       │
                                       ▼
                          inference/ (live stream + model → decisions)
```

Three ingestion domains that never share code paths: **Market** (Yahoo, minute OHLCV → `market.ohlcv.1m`), **EDGAR** (10-K/10-Q/8-K + XBRL → `edgar.filings`, incremental via daily-index + watermark — see `docs/ingestion/edgar-incremental-ingestion.md`), **News** (headlines → `news.sentiment`; not started).

Invariants to preserve:
- Consumers/sinks write **idempotently**; EDGAR dedup keeps the latest `filed_date` per `(cik, metric, period_end)` so amendments supersede.
- **Incremental everywhere** — no full re-pulls, no full-refresh loads.
- Medallion: RAW mirrors landing, SILVER engineers financials/technicals, GOLD is ML-ready marts. ML and MCP read **only** from GOLD.
- Scope: equities only (S&P 500), minute-level, free data sources only; decisions are emitted, never auto-traded.

## Conventions & gotchas

- **SEC EDGAR / Wikipedia** need an honest `User-Agent` (403 otherwise) and EDGAR is capped at 10 req/s. Get it from `src.utils.env.get_sec_user_agent()` (reads `SEC_USER_AGENT`, raises if unset) — don't hardcode one. `src/utils/env.load_env()` is the single `.env` loader; never `load_dotenv` an absolute path.
- The dbt profile `aurum_dwh` (`~/.dbt/profiles.yml`) targets local **Postgres** `aurum`, schema `bronze`. A separate `aurum` profile points at Snowflake. Schemas are set per layer by `generate_schema_name` (`macros/`), which is overridden so `bronze`/`silver`/`gold` are used verbatim rather than prefixed with the profile schema.
- **SonarCloud** gates every push/PR (`sonar-project.properties`, org `analyst-ninja`); `docs/**` and `nbs/**` excluded.
- CI (`.github/workflows/ci.yml`) runs on `main`, `develop`, `epic/*`, and PRs — **lint + Sonar only**, tests are commented out. `terraform.yml` validates `infra/terraform/**`, which doesn't exist yet; plan/apply is deliberately local-only (`docs/operations/infra-as-code.md` §5).
- GitHub Actions are pinned by full commit SHA — keep that when editing workflows.
- `nbs/` notebooks are exploration only; several source files still carry `if __name__ == "__main__":` scratch blocks with absolute `/Users/codebase/...` paths — don't copy that pattern.

## Documentation map

`docs/` is grouped by subject — `architecture/` (the target system), `ingestion/` + `warehouse/` (as built), `warehouse/rationale/` (why each model is shaped that way), `operations/`, `design-specs/` (dated history). `docs/README.md` is the index.

`docs/architecture/TECHNICAL_SPEC.md` (spec, build phases, target layout) · `docs/warehouse/data-dictionary.md` (fields per layer) · `docs/warehouse/dwh-medallion.md` (the warehouse **as built**: layer map, model DAG, feature catalogue with formulas, the point-in-time lag decision, the incremental-lookback rule, how to add a feature, the SHAP loop, known approximations — the plan doc it replaced is deleted) · `docs/warehouse/rationale/bronze-models-rationale.md` + `docs/warehouse/rationale/silver-staging-models-rationale.md` + `docs/warehouse/rationale/silver-intermediate-models-rationale.md` + `docs/warehouse/rationale/gold-models-rationale.md` (the four layers **as built** — why each model is shaped the way it is, finance terms explained for non-finance readers; the intermediate doc carries the incremental-lookback/warm-up rules and the full-vs-incremental verification recipe; the gold doc carries the cross-sectional transform, the target/leakage contract and the walk-forward fold rule) · `docs/warehouse/rationale/concept-map-rationale.md` (why each XBRL concept is mapped/dropped/ranked in `seeds/concept_map.csv`, with measured coverage) · `docs/warehouse/rationale/selected-features-seed.md` (the SHAP feature-selection loop; why `seeds/selected_features.csv` must exist before any model is trained) · `docs/ingestion/edgar-incremental-ingestion.md` · `docs/operations/infra-as-code.md` (Terraform for Snowflake/Kafka/Postgres) · `docs/operations/cicd.md` · `docs/ingestion/datasource-framework.md` (ingestion framework as built: registry/factory/feed flow, config reference, rough edges) · `repo_structure.md` (aspirational tree — does not match `src/`).
