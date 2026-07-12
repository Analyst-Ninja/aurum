# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

AURUM is early in implementation (Phase 0 — infra bootstrap). Design is complete (spec v2.0); most of the target system does not exist yet. `main.py` is a placeholder and `src/` currently holds only Yahoo Finance datasource stubs (`batch_ds.py` still has `pass`-body methods). Treat `docs/TECHNICAL_SPEC.md` as the source of truth for what to build and where it goes — build phases and target repo layout live in sections 5–6.

## Commands

Package/venv managed with **uv** (Python 3.12). The CI (`.github/workflows/ci.yml`) is the canonical command set:

```bash
uv sync --locked --no-build          # install deps from uv.lock
uv run ruff check src/ main.py       # lint (ruff)
uv run ruff check --fix src/ main.py # lint + autofix
uv run pytest tests/ -v              # tests (tests/ dir does not exist yet)
uv run pytest tests/path::test_name  # run a single test
uv run main.py                       # run entrypoint (currently a placeholder)
```

When adding a dependency, use `uv add <pkg>` so `pyproject.toml` and `uv.lock` stay in sync — CI runs `--locked` and fails on drift.

## Architecture

AURUM is a streaming financial-data platform with a Kafka backbone and two consumption paths. Data flows one direction through distinct stages, each a separate `src/` package (see spec §5):

```
datasources/apis  →  producers  →  Kafka topics  →  consumers  →  Postgres (landing)
                                                                      │ Airflow incremental load
                                                                      ▼
                                                    Snowflake  RAW → SILVER → GOLD  (dbt medallion)
                                                                      │
                                        ┌─────────────────────────────┴───────────────┐
                                        ▼                                               ▼
                              ml/ (train + SHAP)                              mcp_server/ (FastMCP NL→SQL)
                                        │
                                        ▼
                        inference/ (live stream + model → trading decisions)
```

Three ingestion domains, each with its own producer→topic→consumer chain — they never share code paths:
- **Market** — `yahoo` websocket, minute OHLCV → `market.ohlcv.1m`
- **EDGAR** — SEC 10-K/10-Q/8-K + XBRL facts → `edgar.filings`, ingested **incrementally** (daily-index + watermark, never re-pulls history — see `docs/edgar-incremental-ingestion.md`)
- **News** — headlines scored by a sentiment classifier → `news.sentiment`

Key architectural invariants:
- **Consumers write idempotently** to Postgres; the EDGAR dedup rule keeps the latest `filed_date` per `(cik, metric, period_end)` so amendments supersede.
- **Incremental everywhere** — Airflow loads Postgres→Snowflake incrementally, EDGAR never does full re-pulls. Don't introduce full-refresh logic.
- The **medallion** (dbt): RAW mirrors landing, SILVER engineers financials/technicals, GOLD is ML-ready feature marts. Both the ML pipeline and the MCP server read only from GOLD.
- Scope is **equities only** (S&P 500), minute-level (no tick data), decisions are emitted not auto-traded. All data sources are free — no paid vendors.

## Conventions & gotchas

- **SEC EDGAR** requires an honest `User-Agent` and is rate-limited to 10 req/s — respect this in any EDGAR client.
- **SonarCloud** quality gate runs on every push/PR (`sonar-project.properties`, org `analyst-ninja`); `docs/**` and `nbs/**` are excluded from analysis.
- CI runs on `main`, `epic/*`, and PRs. GitHub Actions pin third-party actions by full commit SHA — keep that when editing workflows.
- Notebooks in `nbs/` are exploration only, not part of the shipped system.

## Documentation map

`docs/TECHNICAL_SPEC.md` (full spec, build phases, repo layout) · `docs/data-dictionary.md` (every field, layer by layer) · `docs/edgar-incremental-ingestion.md` · `docs/infra-as-code.md` (Terraform for Snowflake/Kafka/Postgres) · `docs/cicd.md`.
