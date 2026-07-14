"""YahooOhlcvDataSource: pure fetch — chunks symbols, normalizes, validates."""

from datetime import date

import pandas as pd

from src.core.interfaces import FetchRequest
from src.datasources.apis.yahoo import ohlcv_ds
from src.datasources.apis.yahoo.config import OHLCVDailyConfig
from src.datasources.apis.yahoo.ohlcv_ds import YahooOhlcvDataSource
from src.datasources.apis.yahoo.schemas import OhlcvBar

REQ = FetchRequest(
    symbols=("AAPL", "MSFT", "NVDA"), start=date(2020, 1, 1), end=date(2020, 1, 10)
)


class FakeTickers:
    """Stands in for yf.Tickers; records requested symbols."""

    calls: list[list[str]] = []

    def __init__(self, symbols):
        self.symbols = list(symbols)
        FakeTickers.calls.append(self.symbols)

    def history(self, start, end, interval, auto_adjust):
        # Yahoo-shaped multi-index frame: (field, ticker) columns
        fields = [
            "Adj Close",
            "Close",
            "Dividends",
            "High",
            "Low",
            "Open",
            "Stock Splits",
            "Volume",
        ]
        cols = pd.MultiIndex.from_product([fields, self.symbols])
        idx = pd.DatetimeIndex([pd.Timestamp("2020-01-02")], name="Date")
        return pd.DataFrame(1.0, index=idx, columns=cols)


def _source(batch_size=2):
    FakeTickers.calls = []
    return YahooOhlcvDataSource(OHLCVDailyConfig(batch_size=batch_size))


def test_schema_is_ohlcv_bar():
    assert YahooOhlcvDataSource.schema is OhlcvBar


def test_chunks_symbols_by_batch_size(monkeypatch):
    src = _source(batch_size=2)
    monkeypatch.setattr(ohlcv_ds.yf, "Tickers", FakeTickers)

    frames = list(src.fetch(REQ))

    assert FakeTickers.calls == [["AAPL", "MSFT"], ["NVDA"]]
    assert len(frames) == 2


def test_frames_conform_to_schema(monkeypatch):
    src = _source(batch_size=3)
    monkeypatch.setattr(ohlcv_ds.yf, "Tickers", FakeTickers)

    frame = next(iter(src.fetch(REQ)))

    assert list(frame.columns) == OhlcvBar.frame_columns()
    assert set(frame["symbol"]) == {"AAPL", "MSFT", "NVDA"}


def test_empty_response_yields_nothing(monkeypatch):
    class EmptyTickers(FakeTickers):
        def history(self, **kw):
            return pd.DataFrame()

    src = _source(batch_size=3)
    monkeypatch.setattr(ohlcv_ds.yf, "Tickers", EmptyTickers)

    assert list(src.fetch(REQ)) == []
