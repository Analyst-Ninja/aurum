# AURUM — Datasource Framework Design

**Date:** 2026-07-13
**Status:** Approved design
**Scope:** How every datasource in AURUM is structured, configured, tested, and extended. Governs `src/core/`, `src/datasources/`, `src/pipelines/`, and `configs/`. Does **not** change the platform architecture in `docs/TECHNICAL_SPEC.md` — datasources remain the API-client layer feeding producers/pipelines.

---

## 1. Problem

Three ingestion domains (Yahoo market data, SEC EDGAR filings, news) each need client code. The current code has structural problems that will compound with every new source:

- `BaseAPIDataSource` mixes fetching from an API with writing to Postgres (`write_data` hardcodes the `ohlcv_1d` table) — every source is coupled to one sink, and Kafka producers (Phase 1–2) can't reuse the fetch logic.
- Watermark/incremental logic is embedded inside `OHLCVDataSource.read_data`, so EDGAR and news would each reinvent it.
- HTTP concerns (User-Agent, rate limits, retries, timeouts) are copy-pasted per call site; the SEC UA is currently a placeholder string.
- Configuration is scattered (env vars, hardcoded defaults, magic numbers in method signatures).

This spec defines the framework once so that adding a datasource is a recipe, not a design exercise.

## 2. Design principles

1. **Datasource = API client only.** It fetches, normalizes, and emits typed data. It never writes to a database, never publishes to Kafka, never sleeps in loops. Sinks and orchestration are injected.
2. **Small frozen interfaces.** Four protocols (`BatchDataSource`, `StreamingDataSource`, `StateStore`, `Sink`) are the contract everything composes through. They are designed to be stable; implementations behind them are replaceable (yfinance is explicitly "replaceable adapter" per TECHNICAL_SPEC §4).
3. **Cadence-agnostic incrementality.** A pipeline run works identically whether it runs daily, monthly, or quarterly: read watermark, compute the gap, fetch the gap, advance watermark after a successful write. EDGAR especially — filings arrive quarterly per company and the daily index catches up over any gap length.
4. **Configuration is typed and validated at startup.** pydantic-settings models per source, values layered YAML → env → `.env`. Invalid config fails fast with a clear error, never mid-run.
5. **Schemas are the single source of truth.** One pydantic record model per landing table, mirroring `docs/data-dictionary.md`, carrying its own natural key. Sinks, dedup, and validation all read from the schema.
6. **Idempotent by construction.** Sinks upsert on natural keys (`ON CONFLICT DO NOTHING`), so replays, retries, and overlapping fetch windows are safe.

## 3. Repository layout

```
src/
├── core/                        # shared kernel — no source-specific code, no upward deps
│   ├── config.py                # YAML + env layering via pydantic-settings
│   ├── schemas.py               # BaseRecord + shared validators
│   ├── interfaces.py            # the four protocols + FetchRequest
│   ├── state.py                 # PostgresStateStore, InMemoryStateStore
│   ├── sinks.py                 # PostgresSink (KafkaSink added in Phase 1–2)
│   ├── http.py                  # shared session: UA, timeout, retry, rate limiter
│   └── errors.py                # DataSourceError hierarchy
├── datasources/apis/
│   ├── yahoo/
│   │   ├── config.py            # YahooConfig
│   │   ├── schemas.py           # OhlcvBar
│   │   ├── batch_ds.py          # YahooOhlcvDataSource (fetch only)
│   │   └── realtime_ds.py       # YahooRealtimeDataSource (streaming)
│   ├── edgar/                   # config.py / schemas.py / batch_ds.py — same shape
│   └── news/                    # same shape, built when news API is chosen
├── pipelines/                   # composition layer: source + state + sink
│   └── ohlcv_daily.py
configs/
├── base.yaml                    # shared: logging, universe source
├── yahoo.yaml
└── edgar.yaml
tests/
├── fakes.py                     # InMemoryStateStore, ListSink, canned HTTP responses
├── fixtures/                    # recorded API payloads (JSON, master.idx samples)
├── test_yahoo_ohlcv.py
└── test_pipeline_ohlcv.py
```

Rule: `core/` imports nothing from `datasources/` or `pipelines/`. `datasources/` imports only `core/`. `pipelines/` composes both. When Kafka producers arrive (Phase 1–2), a producer is just another pipeline: same datasource, `KafkaSink` instead of `PostgresSink`.

## 4. Core interfaces

`src/core/interfaces.py`:

```python
"""Contracts every datasource, sink, and state store implements."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import ClassVar, Protocol, TypeVar

import pandas as pd

from src.core.schemas import BaseRecord

TRecord = TypeVar("TRecord", bound=BaseRecord)


@dataclass(frozen=True)
class FetchRequest:
    """What to fetch. Computed by the pipeline (from watermarks), honored by the source.

    `start`/`end` bound the window; `end` is exclusive. The window can span a day,
    a month, or several quarters — sources must not assume a cadence.
    """

    symbols: tuple[str, ...]
    start: date
    end: date
    interval: str = "1d"


class BatchDataSource(Protocol[TRecord]):
    """Pull-based source. Pure read: no DB, no Kafka, no sleep loops."""

    schema: ClassVar[type[TRecord]]

    def fetch(self, request: FetchRequest) -> Iterator[pd.DataFrame]:
        """Yield DataFrames whose columns conform to `schema`.

        Yield in chunks (per API batch) so the pipeline can write incrementally —
        bounded memory, resume-safe.
        """
        ...


class StreamingDataSource(Protocol[TRecord]):
    """Push-based source (websocket). Yields validated records one at a time."""

    schema: ClassVar[type[TRecord]]

    async def subscribe(self, symbols: list[str]) -> None: ...
    def stream(self) -> AsyncIterator[TRecord]: ...
    async def close(self) -> None: ...


class StateStore(Protocol):
    """Watermark storage keyed by (source, entity).

    entity granularity is per-source: per-symbol for OHLCV, a single
    'daily_index' entity for EDGAR, per-feed for news.
    """

    def get_watermark(self, source: str, entity: str) -> datetime | None: ...
    def set_watermark(self, source: str, entity: str, value: datetime) -> None: ...


class Sink(Protocol):
    """Idempotent writer. Upserts on the record's natural key."""

    def write(self, df: pd.DataFrame, *, table: str, natural_key: list[str]) -> int:
        """Write rows; ON CONFLICT DO NOTHING on natural_key. Returns rows written."""
        ...
```

Why an *iterator of DataFrames* for batch: preserves today's fetch-batch → write-batch behavior (a crash mid-run loses nothing already written; the next run's watermarks skip it), keeps memory bounded for 500 symbols × 25 years, and stays pandas-native for bulk paths. Streaming yields single validated records because each becomes one Kafka message (TECHNICAL_SPEC §3.1 message shapes).

## 5. Record schemas

`src/core/schemas.py`:

```python
"""Base record model. One subclass per landing table, mirroring docs/data-dictionary.md."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class BaseRecord(BaseModel):
    """A single row of a landing table.

    Subclasses declare `table` and `natural_key`; sinks and dedup read them
    from the schema so the key is defined exactly once.
    """

    model_config = ConfigDict(frozen=True)

    table: ClassVar[str]
    natural_key: ClassVar[list[str]]

    @classmethod
    def validate_frame(cls, df: "pd.DataFrame") -> "pd.DataFrame":
        """Cheap batch validation: required columns present, no row-by-row instantiation."""
        missing = set(cls.model_fields) - set(df.columns)
        if missing:
            raise SchemaMismatchError(f"{cls.__name__}: missing columns {sorted(missing)}")
        return df[list(cls.model_fields)]
```

Example source schema — `src/datasources/apis/edgar/schemas.py`:

```python
from datetime import date
from typing import ClassVar

from pydantic import field_validator

from src.core.schemas import BaseRecord


class EdgarFact(BaseRecord):
    """One reported value per metric per filing (landing.edgar_facts)."""

    table: ClassVar[str] = "edgar_facts"
    natural_key: ClassVar[list[str]] = [
        "cik", "metric", "period_end", "form_type", "accession_no",
    ]

    ticker: str
    cik: str            # 10-digit zero-padded
    metric: str
    value: float        # raw dollars — never rescale on ingest
    period_end: date
    filed_date: date
    form_type: str
    accession_no: str

    @field_validator("cik")
    @classmethod
    def _pad_cik(cls, v: str) -> str:
        return v.zfill(10)
```

`OhlcvBar` (yahoo) and `NewsItem` (news) follow the same pattern with the keys from the data dictionary: `(ticker, ts)` and `(source, url, ts)`.

## 6. Configuration

Layering (later wins): `configs/base.yaml` → `configs/<source>.yaml` → environment variables → `.env`. Secrets (`POSTGRES_URL`, `EDGAR_USER_AGENT`) live only in env/`.env`, never YAML. New deps: `uv add pydantic-settings pyyaml`.

`configs/edgar.yaml`:

```yaml
forms: ["10-K", "10-Q", "8-K"]
rate_limit_per_sec: 8        # SEC hard limit is 10; leave headroom
request_timeout_sec: 30
daily_index_base: "https://www.sec.gov/Archives/edgar/daily-index"
```

`src/datasources/apis/edgar/config.py`:

```python
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from src.core.config import yaml_settings_source


class EdgarConfig(BaseSettings):
    """EDGAR client settings. SEC invariants are enforced here, by construction."""

    model_config = {"env_prefix": "EDGAR_"}

    user_agent: str = Field(..., description="'AURUM-Project <real email>' — SEC 403s without it")
    forms: list[str] = ["10-K", "10-Q", "8-K"]
    rate_limit_per_sec: float = Field(8, le=10)   # cannot exceed SEC's 10 req/s
    request_timeout_sec: int = 30
    daily_index_base: str = "https://www.sec.gov/Archives/edgar/daily-index"

    @field_validator("user_agent")
    @classmethod
    def _honest_ua(cls, v: str) -> str:
        if "@" not in v or "example" in v or "address.com" in v:
            raise ValueError("EDGAR_USER_AGENT must contain a real contact email")
        return v
```

A missing `EDGAR_USER_AGENT` or a rate limit above 10 is a startup error, not a runtime 403.

## 7. Cross-cutting infrastructure (`core/`, never per-source)

### 7.1 HTTP

```python
# src/core/http.py
def make_session(*, user_agent: str, rate_limit_per_sec: float | None = None,
                 timeout_sec: int = 30, retries: int = 3) -> RateLimitedSession:
    """One session factory for every REST datasource.

    - mandatory User-Agent
    - exponential backoff retry on 429/5xx/timeouts
    - token-bucket rate limiter when rate_limit_per_sec is set
    - default timeout on every request
    """
```

No datasource calls `requests` directly, and no datasource contains `time.sleep` — pacing lives in the session.

### 7.2 Errors

```python
# src/core/errors.py
class DataSourceError(Exception): ...
class TransientError(DataSourceError): ...     # retryable: 5xx, timeout, socket drop
class PermanentError(DataSourceError): ...     # not retryable: 403, bad config
class SchemaMismatchError(PermanentError): ...
```

Pipelines retry `TransientError` with backoff; `PermanentError` fails the run loudly.

### 7.3 Logging

Stdlib `logging`, configured once in `core/config.py`; contextual fields in messages (`source=edgar run=2026-07-13 filers=3`). No `print` in `src/`.

## 8. Example: datasource implementation

Yahoo OHLCV after refactor — fetch only, no watermarks, no writes:

```python
# src/datasources/apis/yahoo/batch_ds.py
class YahooOhlcvDataSource:
    """Batch OHLCV bars from Yahoo Finance."""

    schema = OhlcvBar

    def __init__(self, config: YahooConfig):
        self._config = config

    def fetch(self, request: FetchRequest) -> Iterator[pd.DataFrame]:
        for chunk in batched(request.symbols, self._config.batch_size):
            raw = yf.Tickers(list(chunk)).history(
                start=request.start, end=request.end,
                interval=request.interval, auto_adjust=False,
            )
            if raw is None or raw.empty:
                continue
            yield self.schema.validate_frame(self._normalize(raw))

    def _normalize(self, raw: pd.DataFrame) -> pd.DataFrame:
        """Multi-index yfinance frame → long-form data-dictionary columns."""
        ...  # current _read_batch_data reshaping, unchanged
```

EDGAR — same interface, different internals. The daily-index catch-up loop demonstrates cadence-agnosticity: `request.start`→`request.end` may span one day or two quarters, the loop is identical:

```python
# src/datasources/apis/edgar/batch_ds.py
class EdgarFactsDataSource:
    """Incremental EDGAR facts via the daily index (docs/edgar-incremental-ingestion.md)."""

    schema = EdgarFact

    def __init__(self, config: EdgarConfig, session: RateLimitedSession):
        self._config = config
        self._session = session   # rate-limited to <=10 req/s by construction

    def fetch(self, request: FetchRequest) -> Iterator[pd.DataFrame]:
        # Works for any gap: yesterday, last month, or last quarter.
        filers: set[str] = set()
        for day in business_days(request.start, request.end):
            idx = self._fetch_daily_index(day)          # 404 → weekend/holiday → skip
            filers |= self._filter(idx, forms=self._config.forms,
                                   ciks=request.symbols)
        for cik in sorted(filers):
            facts = self._fetch_company_facts(cik)       # full history in response...
            new = facts[facts["filed_date"] >= request.start]   # ...keep only new rows
            if not new.empty:
                yield self.schema.validate_frame(new)
```

## 9. Example: pipeline (composition layer)

Watermark logic, batching cadence, and writes live here — not in the datasource. Today's incremental OHLCV behavior, relocated:

```python
# src/pipelines/ohlcv_daily.py
class OhlcvDailyPipeline:
    """Incremental OHLCV: fetch only bars newer than the landing table."""

    SOURCE = "yahoo_ohlcv"

    def __init__(self, source: BatchDataSource[OhlcvBar],
                 state: StateStore, sink: Sink, config: YahooConfig):
        self._source, self._state, self._sink, self._config = source, state, sink, config

    def run(self, symbols: list[str], end: date | None = None) -> int:
        end = end or datetime.now(timezone.utc).date()   # exclusive
        written = 0
        for start, group in self._group_by_start(symbols, end).items():
            request = FetchRequest(symbols=tuple(group), start=start, end=end)
            for frame in self._source.fetch(request):
                frame = frame.assign(run_date=end, inserted_at=datetime.now(timezone.utc))
                written += self._sink.write(
                    frame,
                    table=self._source.schema.table,
                    natural_key=self._source.schema.natural_key,
                )
                self._advance_watermarks(frame)   # only after a successful write
        return written

    def _group_by_start(self, symbols: list[str], end: date) -> dict[date, list[str]]:
        """Symbol → next start date (watermark + 1 day), grouped; up-to-date symbols dropped."""
        groups: dict[date, list[str]] = {}
        for sym in symbols:
            wm = self._state.get_watermark(self.SOURCE, sym)
            start = self._config.history_floor if wm is None else wm.date() + timedelta(days=1)
            if start < end:
                groups.setdefault(start, []).append(sym)
        return groups
```

The EDGAR pipeline is the same shape with one watermark entity (`"daily_index"`) instead of per-symbol, and it advances the watermark only after all facts for a day are published — matching the producer algorithm in `docs/edgar-incremental-ingestion.md`. Run it daily, monthly, or quarterly; the gap computation is identical.

When Kafka lands, the EDGAR *producer* is this pipeline with `KafkaSink` — the datasource does not change.

## 10. State store

```python
# src/core/state.py
class PostgresStateStore:
    """Watermarks in meta.watermarks (source, entity, watermark, updated_at)."""

    def get_watermark(self, source: str, entity: str) -> datetime | None: ...
    def set_watermark(self, source: str, entity: str, value: datetime) -> None: ...


class InMemoryStateStore:
    """Dict-backed; for tests."""
```

Migration note: day one, the OHLCV pipeline may keep deriving watermarks from `MAX(date) ... GROUP BY symbol` on `ohlcv_1d` behind the `StateStore` interface (a `LandingTableStateStore` impl) — no schema migration required to adopt the framework. `meta.watermarks` becomes the standard when EDGAR arrives, since EDGAR's watermark is not derivable from its landing table cheaply.

## 11. Recipe: adding a new datasource

1. **Schema** — add `src/datasources/apis/<name>/schemas.py` with a `BaseRecord` subclass mirroring the `docs/data-dictionary.md` entry; declare `table` + `natural_key`.
2. **Config** — add `<name>/config.py` (`BaseSettings` subclass, `env_prefix`) and `configs/<name>.yaml`. Bake API invariants (rate limits, required headers) into validators.
3. **Datasource** — implement `fetch()` (batch) or `subscribe/stream/close` (streaming). Use `core.http.make_session` — no direct `requests`, no `time.sleep`, no DB access.
4. **Pipeline** — compose source + `StateStore` + `Sink` in `src/pipelines/<name>_<cadence>.py`. Watermark advance only after successful write.
5. **Tests** — parser unit tests against recorded fixtures in `tests/fixtures/`; pipeline tests with `InMemoryStateStore` + `ListSink`; schema contract test (`validate_frame` on a fixture).
6. **Wire** — entrypoint or Airflow task calls the pipeline. Done.

A new source touches its own package, one YAML file, and tests — nothing in `core/` and nothing in other sources.

## 12. Migration plan (behavior-preserving)

1. Add `src/core/` (interfaces, schemas, config loader, http, errors, state, sinks) + `configs/` + deps (`pydantic-settings`, `pyyaml`).
2. Refactor Yahoo: `_read_batch_data` reshaping → `YahooOhlcvDataSource.fetch()`; watermark grouping + write loop → `OhlcvDailyPipeline`; hardcoded params (`batch_size=100`, `sleep_seconds=10`, `start_date="2000-01-01"`) → `configs/yahoo.yaml`.
3. Port `tests/test_ohlcv_incremental.py` to pipeline tests — same assertions (grouping by watermark, skip up-to-date symbols, write-per-batch, empty-batch skip).
4. Delete `BaseAPIDataSource` (`write_data`, module-level `POSTGRES_URL` engine creation) once nothing imports it.
5. `realtime_ds.py` conforms to `StreamingDataSource` (already close — add `schema`, yield validated `OhlcvBar` records from `stream()`).
6. EDGAR datasource + pipeline built fresh on the recipe (Phase 1); news follows when the API is chosen (Phase 5).

## 13. Testing strategy

- **No network in tests.** Recorded fixtures: yfinance frame snapshots, `master.YYYYMMDD.idx` samples, `companyfacts` JSON excerpts.
- **Fakes over mocks** for interfaces: `InMemoryStateStore`, `ListSink` (captures frames), `CannedSession` (URL → fixture).
- **Contract tests**: every datasource's fixture output passes `schema.validate_frame`; every schema's `natural_key ⊆ model_fields`.
- **Pipeline tests**: watermark math (fresh symbol, gap catch-up, up-to-date skip), idempotent re-run, watermark-not-advanced-on-write-failure.
- CI unchanged: `uv run ruff check src/ main.py && uv run pytest tests/ -v`.

## 14. Scenarios this design absorbs

| Future event | What changes |
|---|---|
| Kafka producers arrive (Phase 1–2) | New `KafkaSink`; pipelines swap sink. Datasources untouched. |
| yfinance breaks | New impl of `BatchDataSource[OhlcvBar]`; pipeline untouched. |
| News API chosen (open question §7.1) | Fill the `news/` slot via the recipe. |
| EDGAR run cadence changes (daily ↔ monthly ↔ quarterly) | Nothing — watermark gap catch-up is cadence-agnostic. |
| Minute bars alongside daily | `FetchRequest.interval`; new pipeline instance, same source. |
| Symbol universe changes (S&P 500 rebalance) | Universe comes from config/`company_meta`, not code. |
| Replays / duplicate deliveries | Idempotent sink upserts on natural keys. |
| A source needs auth headers / API keys | Source config + `.env`; `make_session` already carries headers. |

## 15. Out of scope

- Kafka producer/consumer implementations (Phases 1–2, separate design).
- Airflow DAGs, dbt models, Snowflake loading (TECHNICAL_SPEC §3.5–3.6).
- News sentiment classifier (Phase 5).
- Async batch fetching — sync is sufficient at current volumes; the interface doesn't preclude an async variant later.
