# OHLCV Incremental Ingestion — Watermark-Driven, Rate-Limit-Safe

Date: 2026-07-12
Component: `src/datasources/apis/yahoo/batch_ds.py` (`OHLCVDataSource`)
Supersedes: the CSV ingest log from `2026-07-12-ohlcv-ingest-log-design.md`.

## Problem

`read_data` currently re-fetches full history from `start_date` for every symbol,
and the CSV skip-log permanently skips any symbol once written — so a symbol is
either fully re-pulled or never refreshed. Both are wrong:

- Full re-pulls waste Yahoo API calls (rate-limited; `sleep(10)` per batch).
- The skip-log means logged symbols never get new bars.

We want **incremental** ingestion: each run pulls only bars newer than what the
DB already holds, and symbols already up to date cost **zero** API calls.

This aligns with the project invariant "incremental everywhere — never re-pull
history" and mirrors the EDGAR watermark pattern
(`docs/edgar-incremental-ingestion.md`).

## Decisions (locked)

- **Watermark source:** the DB. `MAX(date)` per symbol from `ohlcv_1d`.
  Authoritative, no drift.
- **CSV log removed entirely.** Delete `_log_dir`, `_log_path`,
  `_load_ingested`, `_log_ingested`, their tests, and the log filter. DB is the
  sole source of truth for what has been ingested.
- **`run_date` / `inserted_at` columns stay** (added when writing batches).
- **Watermark table is `ohlcv_1d`** — the current `write_data` target. This
  feature targets the daily flow.
- **Throttle configurable:** `read_data` gains `sleep_seconds=10`.
- **Empty batch:** if `_read_batch_data` returns an empty frame, skip
  `write_data` for that batch and continue.

## New helper

`_get_watermarks() -> dict[str, date]`

- Requires `POSTGRES_URL`; builds an engine like `write_data`.
- Runs `SELECT symbol, MAX(date) AS max_date FROM ohlcv_1d GROUP BY symbol`.
- Returns `{symbol: date}`.
- If the table does not exist yet (first run) or the query errors on a missing
  table, return `{}`.

## `read_data` algorithm

Signature: `read_data(symbols, start_date, end_date=None, interval="1d",
batch_size=100, sleep_seconds=10)`.

```
watermarks = self._get_watermarks()
today      = datetime.now(timezone.utc).date()
run_date   = today

# Group symbols by the start date each one needs
groups: dict[start -> list[symbol]] = {}
for s in symbols:
    wm = watermarks.get(s)
    if wm is None:
        start = start_date                 # brand-new symbol: full history
    else:
        start = wm + timedelta(days=1)     # incremental: only newer bars
        if start > today:
            continue                       # already up to date: NO API call
    groups.setdefault(start, []).append(s)

batch_frames = []
for start, grp_symbols in groups.items():
    for i in range(0, len(grp_symbols), batch_size):
        batch_symbols = grp_symbols[i:i+batch_size]
        batch_data = self._read_batch_data(batch_symbols, start, end_date, interval)
        if batch_data.empty:
            continue                       # skip no-op write
        batch_data = batch_data.assign(
            run_date=run_date,
            inserted_at=datetime.now(timezone.utc),
        )
        batch_frames.append(batch_data)
        self.write_data(batch_data)
        time.sleep(sleep_seconds)

self.data = pd.concat(batch_frames, ignore_index=True) if batch_frames \
            else pd.DataFrame(columns=[...])   # existing empty-columns frame
return self.data
```

Notes:
- `start_date` stays the floor for brand-new symbols (str "YYYY-MM-DD").
- Group start for incremental symbols is a `date`; yfinance accepts both.
- In steady state every symbol shares the same watermark → a single group → the
  same batching as today, but over a small recent window instead of full
  history.

## Data flow

DB `MAX(date)` per symbol → per-symbol start date → group by start → batched
`_read_batch_data` → stamp `run_date`/`inserted_at` → `write_data` → Postgres.

## Rate-limit behavior

- Up-to-date symbols issue **zero** requests (skipped before batching).
- Remaining symbols fetch a narrow window (small payload).
- `sleep_seconds` throttles between batches (default 10).

## Error handling

- Missing `ohlcv_1d` / first run → `_get_watermarks` returns `{}` → everything
  treated as brand-new (full history from `start_date`).
- `write_data` failure propagates (unchanged); the run stops, next run resumes
  from the DB watermark — inherently resumable, no separate log needed.
- Empty batch frame → no write, continue.

## Testing

Mock `_read_batch_data`, `write_data`, and `_get_watermarks` (and
`time.sleep`) so no network/DB is hit.

- `_get_watermarks` returns `{}` when the table is missing (patch `pd.read_sql`
  to raise a DB error) — assert empty dict, no exception.
- Brand-new symbols (empty watermarks) → fetched with `start = start_date`.
- Symbol with watermark `d` → fetched with `start = d + 1 day`.
- Symbol whose `watermark + 1 > today` → **not** fetched (no `_read_batch_data`
  call for it), and not written.
- Symbols with different starts are fetched in separate groups (assert the
  `start` passed per group).
- Empty batch frame → `write_data` not called.
- `run_date` / `inserted_at` still stamped on written frames.
- `sleep_seconds` is honored (patch sleep, assert called with the value).
