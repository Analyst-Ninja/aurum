"""Tests for watermark-driven incremental OHLCV ingestion."""

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from src.datasources.apis.yahoo import batch_ds
from src.datasources.apis.yahoo.batch_ds import OHLCVDataSource

# Raw Yahoo OHLCV columns that read_data selects before writing to the DB
YAHOO_COLS = [
    "Adj Close",
    "Close",
    "Dividends",
    "High",
    "Low",
    "Open",
    "Stock Splits",
    "Volume",
]


@pytest.fixture
def ds(monkeypatch):
    """OHLCVDataSource with sleep neutralized and no real DB/network."""
    source = OHLCVDataSource()
    monkeypatch.setattr(batch_ds.time, "sleep", lambda _s: None)
    return source


@pytest.fixture
def recorder(ds, monkeypatch):
    """Wire read_data's collaborators to in-memory recorders.

    Returns (calls, written) where `calls` is a list of (start, symbols) passed
    to _read_batch_data and `written` is the frames handed to write_data.
    """
    calls = []
    written = []

    def fake_batch(symbols, start, end, interval):
        calls.append((start, list(symbols)))
        n = len(symbols)
        frame = pd.DataFrame(
            {"date": ["2020-01-01"] * n, "symbol": list(symbols)}
        )
        # Match the raw Yahoo columns read_data selects before writing
        for col in YAHOO_COLS:
            frame[col] = 0.0
        return frame

    monkeypatch.setattr(ds, "_read_batch_data", fake_batch)
    monkeypatch.setattr(ds, "write_data", lambda data: written.append(data.copy()))
    return calls, written


# --- _get_watermarks -------------------------------------------------------


def test_get_watermarks_missing_table_returns_empty(ds, monkeypatch):
    monkeypatch.setattr(batch_ds, "POSTGRES_URL", "postgresql://fake/db")
    monkeypatch.setattr(batch_ds, "create_engine", lambda **kw: object())

    def raise_db(*a, **k):
        # pandas wraps the underlying DB error in its own DatabaseError
        raise pd.errors.DatabaseError('relation "ohlcv_1d" does not exist')

    monkeypatch.setattr(batch_ds.pd, "read_sql", raise_db)

    assert ds._get_watermarks() == {}


def test_get_watermarks_parses_rows(ds, monkeypatch):
    monkeypatch.setattr(batch_ds, "POSTGRES_URL", "postgresql://fake/db")
    monkeypatch.setattr(batch_ds, "create_engine", lambda **kw: object())
    frame = pd.DataFrame(
        {"symbol": ["AAPL", "MSFT"], "max_date": ["2020-01-05", "2020-01-06"]}
    )
    monkeypatch.setattr(batch_ds.pd, "read_sql", lambda *a, **k: frame)

    assert ds._get_watermarks() == {
        "AAPL": date(2020, 1, 5),
        "MSFT": date(2020, 1, 6),
    }


# --- read_data incremental behavior ---------------------------------------


def test_new_symbol_fetched_from_start_date(ds, recorder, monkeypatch):
    calls, _ = recorder
    monkeypatch.setattr(ds, "_get_watermarks", lambda: {})

    ds.read_data(["AAPL"], "2000-01-01", interval="1d")

    assert calls == [("2000-01-01", ["AAPL"])]


def test_start_date_defaults_to_full_history_floor(ds, recorder, monkeypatch):
    calls, _ = recorder
    monkeypatch.setattr(ds, "_get_watermarks", lambda: {})

    ds.read_data(["AAPL"], interval="1d")  # no start_date given

    assert calls == [("2000-01-01", ["AAPL"])]


def test_known_symbol_fetched_from_watermark_plus_one_day(ds, recorder, monkeypatch):
    calls, _ = recorder
    wm = datetime.now(timezone.utc).date() - timedelta(days=5)
    monkeypatch.setattr(ds, "_get_watermarks", lambda: {"AAPL": wm})

    ds.read_data(["AAPL"], "2000-01-01", interval="1d")

    assert calls == [(wm + timedelta(days=1), ["AAPL"])]


def test_up_to_date_symbol_is_not_fetched(ds, recorder, monkeypatch):
    calls, written = recorder
    today = datetime.now(timezone.utc).date()
    monkeypatch.setattr(ds, "_get_watermarks", lambda: {"AAPL": today})

    ds.read_data(["AAPL"], "2000-01-01", interval="1d")

    assert calls == []  # watermark+1 > today -> zero API calls
    assert written == []


def test_symbol_current_through_yesterday_is_not_fetched(ds, recorder, monkeypatch):
    # Yahoo `end` is exclusive: last stored bar = yesterday means nothing new
    # to fetch for today, so it must issue zero API calls.
    calls, _ = recorder
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    monkeypatch.setattr(ds, "_get_watermarks", lambda: {"AAPL": yesterday})

    ds.read_data(["AAPL"], "2000-01-01", interval="1d")

    assert calls == []


def test_default_end_date_does_not_crash(ds, recorder, monkeypatch):
    calls, _ = recorder
    monkeypatch.setattr(ds, "_get_watermarks", lambda: {})

    ds.read_data(["AAPL"], "2000-01-01", interval="1d")  # end_date omitted

    assert calls == [("2000-01-01", ["AAPL"])]


def test_symbols_with_different_starts_grouped_separately(ds, recorder, monkeypatch):
    calls, _ = recorder
    wm = datetime.now(timezone.utc).date() - timedelta(days=5)
    monkeypatch.setattr(ds, "_get_watermarks", lambda: {"OLD": wm})

    ds.read_data(["NEW", "OLD"], "2000-01-01", interval="1d")

    starts = {start: syms for start, syms in calls}
    assert starts["2000-01-01"] == ["NEW"]
    assert starts[wm + timedelta(days=1)] == ["OLD"]


def test_empty_batch_is_not_written(ds, monkeypatch):
    monkeypatch.setattr(ds, "_get_watermarks", lambda: {})
    monkeypatch.setattr(
        ds, "_read_batch_data", lambda symbols, start, end, interval: pd.DataFrame()
    )
    written = []
    monkeypatch.setattr(ds, "write_data", lambda data: written.append(data))

    ds.read_data(["AAPL"], "2000-01-01", interval="1d")

    assert written == []


def test_written_frames_get_run_date_and_inserted_at(ds, recorder, monkeypatch):
    _, written = recorder
    monkeypatch.setattr(ds, "_get_watermarks", lambda: {})

    ds.read_data(["AAPL"], "2000-01-01", interval="1d")

    today = datetime.now(timezone.utc).date()
    assert len(written) == 1
    frame = written[0]
    assert set(frame["run_date"]) == {today}
    assert frame["inserted_at"].notna().all()


def test_sleep_seconds_is_honored(ds, recorder, monkeypatch):
    monkeypatch.setattr(ds, "_get_watermarks", lambda: {})
    slept = []
    monkeypatch.setattr(batch_ds.time, "sleep", lambda s: slept.append(s))

    ds.read_data(["AAPL"], "2000-01-01", interval="1d", sleep_seconds=3)

    assert slept == [3]
