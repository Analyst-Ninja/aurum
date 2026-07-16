"""LandingTableStateStore derives watermarks from MAX(date) per symbol."""

from datetime import date

import pandas as pd

from src.core import state
from src.core.state import LandingTableStateStore


def _store():
    return LandingTableStateStore(engine_factory=lambda: object(), table="ohlcv_1d")


def test_no_engine_returns_empty():
    # No POSTGRES_URL configured: treat every symbol as brand-new (old behavior)
    store = LandingTableStateStore(engine_factory=None, table="ohlcv_1d")
    assert store.get_watermarks("yahoo_ohlcv") == {}


def test_missing_table_returns_empty(monkeypatch):
    def raise_db(*a, **k):
        raise pd.errors.DatabaseError('relation "ohlcv_1d" does not exist')

    monkeypatch.setattr(state.pd, "read_sql", raise_db)
    assert _store().get_watermarks("yahoo_ohlcv") == {}


def test_parses_rows(monkeypatch):
    frame = pd.DataFrame(
        {"symbol": ["AAPL", "MSFT"], "max_date": ["2020-01-05", "2020-01-06"]}
    )
    monkeypatch.setattr(state.pd, "read_sql", lambda *a, **k: frame)
    assert _store().get_watermarks("yahoo_ohlcv") == {
        "AAPL": date(2020, 1, 5),
        "MSFT": date(2020, 1, 6),
    }


def test_set_watermark_is_noop(monkeypatch):
    frame = pd.DataFrame({"symbol": [], "max_date": []})
    monkeypatch.setattr(state.pd, "read_sql", lambda *a, **k: frame)
    store = _store()
    store.set_watermark("yahoo_ohlcv", "AAPL", date(2020, 1, 1))  # must not raise
    assert store.get_watermarks("yahoo_ohlcv") == {}
