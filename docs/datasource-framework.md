# AURUM — Datasource Framework Guide

> A developer's map of how datasources are built, wired, and run.
> Design rationale lives in `docs/superpowers/specs/2026-07-13-datasource-framework-design.md`.
> This document is the practical companion: what each piece is, how they connect, and how to add your own.

---

## 1. The one-sentence mental model

A **datasource** only knows how to *fetch* data from an API. A **pipeline** decides *what* to fetch (using watermarks), *where* to put it (a sink), and *how fast* (throttling). They meet through four small interfaces, so any piece can be swapped without touching the others.

```
        knows the API              knows the workflow
      ┌────────────────┐         ┌──────────────────────┐
      │  DataSource    │         │      Pipeline        │
      │  fetch() only  │◄────────│ watermarks + writes  │
      └────────────────┘  used   └──────────────────────┘
                            by
```

---

## 2. How the pieces connect

```mermaid
flowchart TB
    subgraph entry [main.py — wiring]
        CFG["CoreConfig + YahooConfig<br/>(reads .env + configs/*.yaml)"]
    end

    subgraph datasource [Datasource layer — pure API client]
        SYM["get_sec_symbols()<br/>symbol universe"]
        DS["YahooOhlcvDataSource<br/>.fetch(FetchRequest)"]
        SCH["OhlcvBar schema<br/>(columns + natural_key)"]
    end

    subgraph pipeline [Pipeline layer — composition]
        PIPE["OhlcvDailyPipeline.run()"]
    end

    subgraph core [Core interfaces — the contracts]
        STATE["StateStore<br/>(watermarks)"]
        SINK["Sink<br/>(writes)"]
    end

    PG[("Postgres<br/>ohlcv_1d")]
    YF["Yahoo Finance API"]

    CFG --> SYM --> PIPE
    CFG --> DS
    CFG --> PIPE
    PIPE -->|"FetchRequest"| DS
    DS -->|"yfinance"| YF
    DS -->|"validated DataFrame"| PIPE
    DS -.->|"schema.table / .natural_key"| SCH
    PIPE -->|"read watermarks"| STATE
    PIPE -->|"write frames"| SINK
    STATE -->|"MAX(date) query"| PG
    SINK -->|"append rows"| PG
```

**Read it top to bottom:** config builds everything → pipeline asks the datasource for a window → datasource returns validated frames → pipeline stamps + writes them via the sink → watermarks advance so next run skips what's already stored.

---

## 3. The four contracts (`src/core/interfaces.py`)

Everything composes through these. If a class satisfies the shape, it fits — no inheritance required (Python `Protocol`).

| Contract | Method(s) | Who implements it today |
|----------|-----------|-------------------------|
| `BatchDataSource` | `fetch(request) -> Iterator[DataFrame]` | `YahooOhlcvDataSource` |
| `StateStore` | `get_watermarks(source)`, `set_watermark(...)` | `LandingTableStateStore`, `InMemoryStateStore` (tests) |
| `Sink` | `write(df, *, table, natural_key) -> int` | `PostgresSink`, `ListSink` (tests) |
| *(value object)* `FetchRequest` | `symbols, start, end, interval` | — frozen dataclass |

`FetchRequest` is the language the pipeline and datasource speak. The pipeline computes it; the datasource obeys it. `end` is **exclusive**, and the window can span a day or several quarters — the datasource never assumes a cadence.

---

## 4. What each file does

```
src/
├── core/                        # shared kernel — imports nothing above it
│   ├── errors.py                # DataSourceError → Transient / Permanent / SchemaMismatch
│   ├── schemas.py               # BaseRecord: table + natural_key + validate_frame()
│   ├── interfaces.py            # the 4 contracts + FetchRequest
│   ├── config.py                # CoreConfig (.env secrets) + setup_logging()
│   ├── state.py                 # LandingTableStateStore (watermarks from MAX(date))
│   └── sinks.py                 # PostgresSink (append to landing table)
│
├── datasources/apis/yahoo/      # one source = one folder
│   ├── schemas.py               # OhlcvBar (mirrors ohlcv_1d columns exactly)
│   ├── config.py                # YahooConfig (YAML + YAHOO_* env)
│   ├── ohlcv_ds.py              # YahooOhlcvDataSource.fetch() — pure read
│   ├── symbols.py               # get_sec_symbols() — the ticker universe
│   └── realtime_ds.py           # websocket source (not yet on the framework)
│
├── pipelines/
│   └── ohlcv_daily.py           # OhlcvDailyPipeline — glues source + state + sink
│
configs/
├── base.yaml                    # shared (logging)
└── yahoo.yaml                   # interval, batch_size, sleep_seconds, history_floor, table

main.py                          # wires it all together and runs one pass
```

**Layering rule (enforced by convention, checked in CI-style greps):**
`core/` → depends on nothing internal · `datasources/` → depends only on `core/` · `pipelines/` → composes both.

---

## 5. A run, step by step

When you execute `uv run main.py`:

```mermaid
sequenceDiagram
    participant M as main.py
    participant P as OhlcvDailyPipeline
    participant St as LandingTableStateStore
    participant D as YahooOhlcvDataSource
    participant Y as Yahoo API
    participant Sk as PostgresSink

    M->>M: load CoreConfig + YahooConfig (.env, yaml)
    M->>P: run(get_sec_symbols(user_agent))
    P->>St: get_watermarks("yahoo_ohlcv")
    St-->>P: {AAPL: 2026-07-10, ...}  (empty on first run)
    P->>P: group symbols by next start (watermark + 1 day)
    loop each start-date group
        P->>D: fetch(FetchRequest(symbols, start, end))
        loop each batch of batch_size symbols
            D->>Y: yf.Tickers(...).history(start, end)
            Y-->>D: multi-index price frame
            D->>D: _normalize() + schema.validate_frame()
            D-->>P: tidy DataFrame (schema columns)
            P->>P: stamp run_date + inserted_at
            P->>Sk: write(frame, table, natural_key)
            Sk-->>P: rows written
            P->>St: set_watermark(...)  (no-op for landing store)
            P->>P: sleep(sleep_seconds)
        end
    end
    P-->>M: total rows written
```

The **incremental magic** is step *"group symbols by next start"*: a symbol already current through yesterday produces an empty `[start, end)` window and is dropped entirely — zero API calls. That's why a second run right after a first does almost nothing.

---

## 6. The schema is the single source of truth

`OhlcvBar` (in `src/datasources/apis/yahoo/schemas.py`) defines the shape **once**, and everyone else reads from it:

```python
class OhlcvBar(BaseRecord):
    table = "ohlcv_1d"                       # ← the sink writes here
    natural_key = ["date", "symbol"]         # ← dedup / upsert key

    date: dt.date
    symbol: str
    adj_close: float = Field(alias="Adj Close")   # ← DataFrame column is "Adj Close"
    close: float = Field(alias="Close")
    # ... etc
```

- The **datasource** calls `OhlcvBar.validate_frame(df)` — cheap column check, not row-by-row (fast for 500 symbols × 25 years).
- The **pipeline** reads `schema.table` and `schema.natural_key` to tell the sink where and how to write.
- The **alias** (`"Adj Close"`) keeps the DataFrame column names exactly matching the existing `ohlcv_1d` table.

Change a column in one place; the whole chain follows.

---

## 7. Configuration — where values come from

Two config objects, loaded at startup, fail fast if wrong.

| Setting | Class | Source (first wins) | Example |
|---------|-------|---------------------|---------|
| `postgres_url`, `sec_user_agent` | `CoreConfig` | env → `.env` | secrets, never in git |
| `interval`, `batch_size`, `sleep_seconds`, `history_floor`, `table` | `YahooConfig` | `YAHOO_*` env → `.env` → `configs/yahoo.yaml` | tunables, safe in git |

`.env` (secrets — not committed):

```
POSTGRES_URL=postgresql://user:pass@localhost:5432/aurum
SEC_USER_AGENT=AURUM-Project you@example.com
```

`configs/yahoo.yaml` (tunables — committed):

```yaml
interval: "1d"
batch_size: 100
sleep_seconds: 10
history_floor: "2000-01-01"
table: "ohlcv_1d"
```

Override any tunable per-run without editing files:

```bash
YAHOO_BATCH_SIZE=25 YAHOO_SLEEP_SECONDS=2 uv run main.py
```

> **Gotcha learned the hard way:** `.env` is read by pydantic-settings into the *config objects*, not into `os.environ`. Always pull secrets from `CoreConfig`, never `os.getenv`.

---

## 8. Running it

**Tests (no network, no DB):**

```bash
uv run pytest tests/ -v          # 32 tests
uv run ruff check src/ main.py   # lint
```

**Fetch-only smoke test (no DB needed) — see what a datasource emits:**

```bash
uv run python -c "
from src.datasources.apis.yahoo.config import YahooConfig
from src.datasources.apis.yahoo.ohlcv_ds import YahooOhlcvDataSource
from src.core.interfaces import FetchRequest
from datetime import date

src = YahooOhlcvDataSource(YahooConfig(batch_size=2))
req = FetchRequest(symbols=('AAPL', 'MSFT'), start=date(2024, 1, 1), end=date(2024, 1, 10))
for frame in src.fetch(req):
    print(frame)
"
```

**Full pipeline (needs `.env` with `POSTGRES_URL` + `SEC_USER_AGENT`):**

```bash
uv run main.py
```

> First run pulls full history for the entire SEC ticker list — slow and heavy. For a bounded trial, raise `history_floor` in `configs/yahoo.yaml` to a recent date, or fetch-smoke a handful of symbols first.

---

## 9. How to add a new datasource

Every source follows the same recipe. Example: adding EDGAR.

**1. Schema** — `src/datasources/apis/edgar/schemas.py`, mirroring `docs/data-dictionary.md`:

```python
class EdgarFact(BaseRecord):
    table = "edgar_facts"
    natural_key = ["cik", "metric", "period_end", "form_type", "accession_no"]

    ticker: str
    cik: str
    value: float          # raw dollars — never rescale
    period_end: date
    filed_date: date
    # ...
```

**2. Config** — `src/datasources/apis/edgar/config.py` + `configs/edgar.yaml`. Bake API invariants into validators (SEC's 10 req/s limit, mandatory User-Agent).

**3. Datasource** — `src/datasources/apis/edgar/batch_ds.py` implementing `fetch()`:

```python
class EdgarFactsDataSource:
    schema = EdgarFact

    def fetch(self, request: FetchRequest) -> Iterator[pd.DataFrame]:
        # any window (day / month / quarter) — same loop
        for day in business_days(request.start, request.end):
            ...
        yield self.schema.validate_frame(new_facts)
```

No DB access, no `time.sleep`, no direct `requests` juggling — the pipeline and shared HTTP handle those.

**4. Pipeline** — `src/pipelines/edgar_daily.py` composing the source with a `StateStore` and `Sink`. Same shape as `OhlcvDailyPipeline`; EDGAR uses a single watermark entity (`"daily_index"`) instead of per-symbol.

**5. Tests** — reuse `tests/fakes.py` (`InMemoryStateStore`, `ListSink`); add recorded API fixtures. Test the parser and the watermark math separately.

**6. Wire** — call the pipeline from `main.py` or an Airflow task.

You touch **one folder, one YAML file, and tests** — nothing in `core/`, nothing in other sources.

---

## 10. Why it's built this way (the payoff)

| You want to… | You change… | You do NOT touch… |
|--------------|-------------|-------------------|
| Send data to Kafka instead of Postgres | add a `KafkaSink`, swap it in the pipeline | any datasource |
| Replace yfinance (it broke again) | a new `BatchDataSource` impl | the pipeline, the sink |
| Add EDGAR / news | a new source folder + pipeline | Yahoo, core |
| Run EDGAR monthly instead of daily | nothing — watermark gap is cadence-agnostic | everything |
| Pull minute bars too | pass `interval` in the `FetchRequest` | the source code |
| Change the universe (S&P rebalance) | config / symbol source | code |

Small contracts + one responsibility per file = each change stays local and testable.
```
