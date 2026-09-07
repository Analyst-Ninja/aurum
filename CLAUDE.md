# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

AURUM is mid-Phase-0. The **design** (`docs/architecture/TECHNICAL_SPEC.md`, spec v2.0) describes the full target system — Kafka backbone, Snowflake medallion, ML + MCP server. Most of that does not exist yet.

What actually exists and runs today:

- `src/ingestion/` — a working config-driven ingestion framework (Yahoo OHLCV + EDGAR financial statements → **Postgres directly**, no Kafka in the code path yet).
- `src/transformation/aurum_dwh/` — a dbt project pointed at **Postgres** (not Snowflake), with the full medallion **bronze**, **silver** and **gold** layers built and tested: 8 `br_*` mirrors, 3 `stg_*` models, 5 `int_*` feature models, 4 `mart_*` marts, 3 seeds, 237 tests (2 warn on documented real-data outliers, 0 error). The `dbt init` example models are gone. `gold.mart_features` holds ~2.9M rows across 503 symbols, 2000 → today. `docs/warehouse/dwh-medallion.md` documents it as built.
- `src/feed/`, `src/inference/`, `src/mcp/`, `airflow/`, `infra/` — empty `__init__.py` placeholders. `main.py` is empty.
- **`AURUM_NUM_THREADS` overrides LightGBM's thread count.** `ModelParams.num_threads` defaults to 4, tuned for an Apple Silicon laptop whose efficiency cores drag every boosting barrier. Fargate vCPUs are homogeneous, so the ECS train task sets this to its vCPU count; an explicit value in the YAML still beats both.
- **Phase 6 (modeling) is built** (#51–#57). `docs/modeling/` (6 docs) specifies preprocessing, purged walk-forward training, SHAP selection, backtesting and the retraining policy; tracked as epic [#50](https://github.com/Analyst-Ninja/aurum/issues/50) with children #51–#57. Primary target `fwd_ret_5d_excess`, LightGBM regression, flat-file registry under `models/`. `docs/modeling/pipeline-runbook.md` walks the 11-step run.
- `tests/` holds 95 unit tests over `src/modeling/` and `src/ingestion/`, and the pytest step in CI is live (GH-57). `docker/aurum.Dockerfile` + `docker/entrypoint.sh` + `docker-compose.modeling.yml` run **all three** workloads — ingestion, dbt and modelling — against a pinned dependency set. No Airflow/MLflow/serving.
- **AWS deployment is built and running on a schedule** — epic [#71](https://github.com/Analyst-Ninja/aurum/issues/71): RDS Postgres, one image on ECR run as four ECS Fargate task definitions, three Step Functions state machines on EventBridge Scheduler crons. Terraform in `infra/terraform/` (`main.tf`, `rds.tf`, `tasks.tf`, `sfn.tf`, `outputs.tf`). `docs/infra/aws-deployment-plan.md` documents it as built, with the schedule DAG and a troubleshooting table.

  | State machine | Cron (UTC) | States |
  |---|---|---|
  | `aurum-daily-market` | `cron(30 22 ? * MON-FRI *)` | `ingest-market` → `dbt` |
  | `aurum-semimonthly-edgar` | `cron(0 6 1,15 * ? *)` | `ingest-market` → `ingest-edgar` → `dbt` |
  | `aurum-monthly-train` | `cron(0 12 1 * ? *)` | `train` |

  **dbt runs after every ingest.** `gold.mart_features` is only as fresh as the last build, and the EDGAR cron fires on any weekday *or weekend* while the daily machine is MON-FRI only — so the EDGAR machine carries its own `dbt` state rather than waiting for the daily one. It ingests prices before fundamentals because the silver models join the two, and a build over fresh statements and stale prices would be as-of two different dates. On the 1st the three machines run in dependency order: market+EDGAR+dbt 06:00, train 12:00, market+dbt 22:30.

  Each task definition's `command` holds the **whole** logical workflow (`sh -c "a && b && c"`), so the state machines are one or two states rather than the ten the original plan described — see `docs/infra/aws-deployment-plan.md` §2.3 for the trade. `dbt` and `train` deliberately stay separate states: a dbt failure must never reach training. Every ECS state is `ecs:runTask.sync`, which fails on a non-zero container exit, so `src/ingestion/cli.py`'s `sys.exit(1)` is load-bearing. Failures publish to SNS **and** end the execution red.

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

# empty a feed's landing table before a full reload (GH-75). Only the EDGAR task uses it,
# once per config, immediately before that config's load.
uv run python -m src.ingestion.truncate -c src/ingestion/configs/edgar/income_statements_quarterly.yaml
#   -d/--run_date  default today — the date the min_refresh_gap_days staleness gate measures against

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

# modelling (Phase 6 — GH-51..56 built; see docs/modeling/training-and-retraining.md §9).
# `compare` is #55's gate: narrowed-vs-full holdout ICIR + decile spread, from two metrics.json.
uv sync --group modeling
uv run python -m src.modeling.cli train           -c src/modeling/configs/lgbm_xs_excess_5d.yaml
uv run python -m src.modeling.cli evaluate        -c ... --version 20260905-a9b91fe
uv run python -m src.modeling.cli select-features -c ... --version 20260905-a9b91fe
uv run python -m src.modeling.cli backtest        -c ... --version latest   # writes backtest/report.html
uv run python -m src.modeling.cli compare         -c ..._narrow.yaml --version <narrow> --baseline <full>
uv run python -m src.modeling.cli predict         -c ... --version latest --asof 2026-09-05

# all three workloads inside the one pinned image (GH-57, widened in GH-72). The first arg
# after `trainer` picks the workload — ingest | dbt | model — and the rest goes to that CLI.
# Reaches the HOST's Postgres via host.docker.internal; ./models and ./data are bind-mounted.
docker compose -f docker-compose.modeling.yml build trainer
docker compose -f docker-compose.modeling.yml run --rm trainer model train -c src/modeling/configs/lgbm_xs_excess_5d.yaml
docker compose -f docker-compose.modeling.yml run --rm trainer ingest -c src/ingestion/configs/yahoo/ohlcv_1d.yaml -f False
docker compose -f docker-compose.modeling.yml run --rm trainer dbt build --select bronze
```

Add dependencies with `uv add <pkg>` — CI runs `--locked` and fails on `pyproject.toml`/`uv.lock` drift.

**Dependency groups.** `dbt-postgres` sits in a `dbt` group rather than `[project].dependencies`, because dbt is a CLI that `src/` never imports. This keeps it out of the default sync: `dbt-core` pulls `dbt-core-experimental-parser`, which publishes an sdist with no wheel and so cannot install under CI's `--no-build`. **Anything importable by the default runtime path (`src/ingestion`, `main.py`) belongs in `[project].dependencies`; optional subsystems and tooling get their own group.** The rule used to read "anything importable by `src/`"; it was amended for the planned `modeling` group (`lightgbm`, `shap`, `scikit-learn`, …), which `src/modeling/` genuinely imports — pulling a ~400 MB ML stack into the ingestion runtime to satisfy the wording is the worse trade. Unlike `dbt-core`, every modeling dependency ships manylinux wheels, so `--no-build` still holds. See `docs/modeling/training-and-retraining.md` §9.2.

The container image is the one place that relaxes this: `docker/aurum.Dockerfile` runs `uv sync` twice — once with `--no-build --group modeling` (cached, mirrors CI), then again with `--group dbt --group modeling` **without** `--no-build`, because the image has to run dbt. CI's install path is untouched.

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
- **The read streams into the write.** `BaseFeed.run` iterates `input_ds.read_data_chunks(...)` and processes/writes each chunk before fetching the next, so peak memory is one batch rather than the whole pull. `BaseDatasource.read_data_chunks` defaults to yielding the single frame `read_data` returns, so a source that does not page (EDGAR) is unchanged; `OHLCVDataSource` overrides it and yields per `batch_size` symbol batch. **`process()` therefore runs once per chunk and must stay row-wise** — no cross-symbol or cross-date aggregation in a feed.
- **`batch_size` is the memory knob.** For Yahoo it sets both the request size and the size of the frame held in memory: 100 symbols × 26 years is ~650k rows. Lower it for a backfill on a small task.
- **The sink writes in chunks.** `Database.write_data` passes `chunksize=WRITE_CHUNK_ROWS` (50k) to `to_sql`; without it pandas builds the parameter list for the whole frame first, so peak memory tracks row count. That is fine for a daily increment and fatal for a first load — with no watermark to resume from a feed pulls 503 symbols from 2000 to today in one frame, which OOM-killed the 8 GB ingest task (exit 137) on 2026-09-06. Chunking bounds the batch, not the frame; it is not a speed change.
- **The staleness gate skips a run whose landing table is still fresh.** A config carrying
  `min_refresh_gap_days` (the six EDGAR configs set `10`) is checked by
  `src/ingestion/freshness.py` before any fetch: it reads `MAX("RUN_DATE")` from the landing
  table and, if the gap to `run_date` is under the threshold, the feed returns
  `execution_status="SKIPPED_FRESH"` with `row_count=0` and **exits 0** — the CLI only exits
  non-zero on `FAILED`. A missing, empty, or unparseable table is always stale, so a first
  load is never gated. `src/ingestion/truncate.py` runs the same check, and must: truncating
  resets the value the gate measures, so an ungated truncate would make every later run look
  stale. It takes `-d/--run_date` and returns `None` when it skips. The market feeds omit the
  key and are never gated.
- **A fresh database means a full load, whatever `-f` says.** `get_watermarks` returns `{}` when the landing table does not exist, so `-f False` against an empty RDS pulls the entire history. Size the task accordingly — `tasks.tf` sizes `aurum-ingest-market` for daily increments, and a backfill needs `run-task --overrides` with more memory.
- **The sink appends and does not deduplicate.** `Database.write_data` is `to_sql(if_exists="append")` and there is no unique index on `MD5_HASH`. For watermarked feeds that is fine — they only fetch new rows. The EDGAR feeds are `full_load: true` with no watermark columns, so every run re-pulls the whole history and would duplicate ~1.9M rows; `src/ingestion/truncate.py` empties each landing table first, which makes it match the `full_load` contract the config already declares. Run it **per config**, not once for all six: a failure halfway through then leaves one table empty rather than all six.
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
- CI (`.github/workflows/ci.yml`) runs on `main`, `develop`, `epic/*`, and PRs — ruff + pytest + Sonar. The install step is `uv sync --locked --no-build --group modeling`, because `tests/modeling/` imports the ML stack. `terraform.yml` validates `infra/terraform/**`, which doesn't exist yet; plan/apply is deliberately local-only (`docs/operations/infra-as-code.md` §5).
- GitHub Actions are pinned by full commit SHA — keep that when editing workflows.
- `nbs/` notebooks are exploration only; several source files still carry `if __name__ == "__main__":` scratch blocks with absolute `/Users/codebase/...` paths — don't copy that pattern.

## Documentation map

`docs/` is grouped by subject — `architecture/` (the target system), `ingestion/` + `warehouse/` (as built), `warehouse/rationale/` (why each model is shaped that way), `modeling/` (Phase 6 — as built), `operations/`, `design-specs/` (dated history). `docs/README.md` is the index.

`docs/architecture/TECHNICAL_SPEC.md` (spec, build phases, target layout) · `docs/warehouse/data-dictionary.md` (fields per layer) · `docs/warehouse/dwh-medallion.md` (the warehouse **as built**: layer map, model DAG, feature catalogue with formulas, the point-in-time lag decision, the incremental-lookback rule, how to add a feature, the SHAP loop, known approximations — the plan doc it replaced is deleted) · `docs/warehouse/rationale/bronze-models-rationale.md` + `docs/warehouse/rationale/silver-staging-models-rationale.md` + `docs/warehouse/rationale/silver-intermediate-models-rationale.md` + `docs/warehouse/rationale/gold-models-rationale.md` (the four layers **as built** — why each model is shaped the way it is, finance terms explained for non-finance readers; the intermediate doc carries the incremental-lookback/warm-up rules and the full-vs-incremental verification recipe; the gold doc carries the cross-sectional transform, the target/leakage contract and the walk-forward fold rule) · `docs/warehouse/rationale/concept-map-rationale.md` (why each XBRL concept is mapped/dropped/ranked in `seeds/concept_map.csv`, with measured coverage) · `docs/warehouse/rationale/selected-features-seed.md` (the SHAP feature-selection loop; why `seeds/selected_features.csv` must exist before any model is trained) · `docs/modeling/modeling-design.md` (Phase 6 entry point: the cross-sectional framing, why `fwd_ret_5d_excess` and not the raw return — SHAP on the raw target collapses onto market-regime columns — why LightGBM, the metrics that replace RMSE, the seven known biases) + `docs/modeling/preprocessing-contract.md` (the one rule: every step must replay against `mart_features` alone; the three deny-lists including the non-stationary-levels argument; keep-the-NaNs) + `docs/modeling/training-and-retraining.md` (purge/embargo and why the naive `fold_id <= k` split leaks; the hand-specified grid; the flat-file registry; three retrain triggers; the two-sided promotion gate) + `docs/modeling/feature-selection-shap.md` (what writes `selected_features.csv`) + `docs/modeling/backtesting.md` (overlapping tranches, cost sweep to a break-even bps, factor attribution, the randomization/signal-lag/deflated-Sharpe checks) · `docs/ingestion/edgar-incremental-ingestion.md` · `docs/operations/training-container.md` (the training image and compose service — host-Postgres networking, bind mounts, non-root, troubleshooting) · `docs/operations/infra-as-code.md` (Terraform for Snowflake/Kafka/Postgres) · `docs/operations/cicd.md` · `docs/ingestion/datasource-framework.md` (ingestion framework as built: registry/factory/feed flow, config reference, rough edges) · `repo_structure.md` (aspirational tree — does not match `src/`).
