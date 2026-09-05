# OHLCV Ingest Log — Resume / Skip Already-Written Symbols

Date: 2026-07-12
Component: `src/datasources/apis/yahoo/batch_ds.py` (`OHLCVDataSource`)

## Problem

`OHLCVDataSource.read_data` fetches OHLCV data for a symbol list in batches and
writes each batch to Postgres (`write_data`). A re-run re-fetches and re-inserts
every symbol from scratch — wasting Yahoo API calls (rate-limited, `sleep(10)`
per batch) and double-inserting into the `ohlcv_1d` table.

We want a durable, file-based record of which symbols have already been written,
so a re-run skips them.

## Solution overview

Maintain a per-interval CSV log under `logs/ohlcv/`. After each batch is
successfully written to the DB, append the batch's requested symbols to the
log. At the start of `read_data`, load the log for the requested interval and
drop already-logged symbols before batching.

The batch is the unit of both DB write and log append, so the log follows a
successful `write_data` call one-to-one.

## Decisions (locked)

- **Log key:** `symbol + interval`. One CSV per interval; `symbol` identifies a
  row within it.
- **What is logged:** *all* requested symbols in a batch, including symbols that
  returned zero rows from Yahoo. Zero-row symbols are treated as done (not
  retried).
- **Location:** `logs/ohlcv/{interval}.csv`, one file per interval.
- **Cadence:** one file append per batch (~`batch_size` rows), after
  `write_data` succeeds.
- **Log lives with the DB insert** (in `read_data`, where `write_data` is
  called), so "log follows successful insert" holds. `_read_batch_data` is
  fetch-only and unchanged.

## File format

`logs/ohlcv/{interval}.csv`

| column        | type   | description                          |
|---------------|--------|--------------------------------------|
| `symbol`      | str    | ticker symbol                        |
| `interval`    | str    | interval (redundant w/ filename; audit) |
| `ingested_at` | str    | ISO-8601 UTC timestamp of the append |

Header written only when the file is first created.

## New helpers on `OHLCVDataSource`

- `_log_dir() -> Path` — returns `Path("logs/ohlcv")`; `mkdir(parents=True,
  exist_ok=True)`.
- `_log_path(interval) -> Path` — `logs/ohlcv/{interval}.csv`.
- `_load_ingested(interval) -> set[str]` — read the CSV if it exists, return the
  set of `symbol` values. Missing file → empty set.
- `_log_ingested(symbols, interval) -> None` — append one row per symbol
  (`symbol`, `interval`, `ingested_at`). Write header only if the file does not
  yet exist.

## Changes to `read_data`

1. At entry: `done = self._load_ingested(interval)`;
   `symbols = [s for s in symbols if s not in done]`.
2. If `symbols` is empty after filtering: skip the batch loop, set `self.data`
   to the empty-columns frame, and return it.
3. In the batch loop, immediately after `self.write_data(batch_data)` succeeds:
   `self._log_ingested(batch_symbols, interval)`.
4. Rest of the loop (`batch_frames`, `sleep(10)`, concat) unchanged.

## Ingestion metadata columns

Before each batch is written to the DB, `read_data` stamps two columns:

- `run_date` — the date the `read_data` run started (UTC). Constant for every
  batch/row in one invocation; groups rows by ingestion run.
- `inserted_at` — UTC timestamp taken at each batch write.

## Error handling / resumability

- Log append happens **after** `write_data` returns. If `write_data` raises, the
  batch is not logged and is re-fetched on the next run.
- Known trade-off: if the process dies after the DB write but before the log
  append, that batch is re-fetched and re-inserted next run — a possible
  duplicate insert into `ohlcv_1d` (the table has no idempotency guard today).
  Accepted for now; batch is the atomic unit.
- `logs/ohlcv/` is created on demand via `mkdir(parents=True, exist_ok=True)`.

## Testing

- `_load_ingested` on a missing file → empty set.
- `_log_ingested` creates dir + file with header on first call; appends without
  header on subsequent calls.
- Round-trip: `_log_ingested` then `_load_ingested` returns the logged symbols.
- `read_data` filters out already-logged symbols (mock `_read_batch_data` /
  `write_data`), and calls `_log_ingested` once per written batch with the
  batch's symbols.
- `read_data` with all symbols already logged skips the loop and returns the
  empty frame.

Mock `write_data` and `_read_batch_data` in tests so no network / DB is hit.
