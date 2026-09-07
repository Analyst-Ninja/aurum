"""The Yahoo OHLCV source: watermark grouping, batching, and the multi-index reshape.

The batching is the memory knob — `read_data_chunks` yields one frame per symbol batch
so peak memory is a batch rather than 503 symbols x 26 years. `read_data` concatenates
and exists only for callers that genuinely want the whole pull.
"""

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from src.ingestion.datasources.api.yahoo import ohlcv


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(ohlcv, "get_sec_user_agent", lambda: "AURUM you@example.com")
    return ohlcv.OHLCVDataSource(
        {
            "type": "yahoo_ohlcv",
            "name": "ohlcv",
            "batch_size": 2,
            "history_floor": "2000-01-01",
            "timeout": 11,
        }
    )


# `None` is a real response yfinance can return, so the "use the default frame"
# sentinel cannot be None.
_DEFAULT = object()


@pytest.fixture
def universe(monkeypatch):
    """Patch the symbol list and record every `yf.Tickers(...).history(...)` call."""
    calls = []

    def _set(symbols, frame=_DEFAULT):
        monkeypatch.setattr(ohlcv, "get_snp500_symbols", lambda agent, timeout: list(symbols))

        class _Tickers:
            def __init__(self, tickers):
                self.tickers = tickers

            def history(self, **kwargs):
                calls.append({"tickers": self.tickers, **kwargs})
                return _raw(self.tickers) if frame is _DEFAULT else frame

        monkeypatch.setattr(ohlcv.yf, "Tickers", _Tickers)
        return calls

    return _set


def _raw(tickers):
    """A miniature of what `yf.Tickers.history` returns: (field, ticker) columns."""
    index = pd.to_datetime(["2026-09-04", "2026-09-05"])
    columns = pd.MultiIndex.from_product([["Close", "Volume"], tickers])
    values = [[float(i + j) for j in range(len(columns))] for i in range(len(index))]
    return pd.DataFrame(values, index=index, columns=columns)


def test_the_user_agent_and_timeout_come_from_the_config(source):
    assert source.user_agent == "AURUM you@example.com"
    assert source.timeout == 11


def test_the_timeout_falls_back_to_the_documented_default(monkeypatch):
    monkeypatch.setattr(ohlcv, "get_sec_user_agent", lambda: "AURUM you@example.com")

    assert ohlcv.OHLCVDataSource({"name": "ohlcv"}).timeout == 100


def test_a_batch_of_two_splits_three_symbols_into_two_requests(source, universe):
    calls = universe(["AAPL", "MSFT", "NVDA"])

    chunks = list(source.read_data_chunks("2026-09-06"))

    assert [call["tickers"] for call in calls] == [["AAPL", "MSFT"], ["NVDA"]]
    assert len(chunks) == 2


def test_a_watermarked_symbol_starts_the_day_after_its_watermark(source, universe):
    calls = universe(["AAPL"])

    list(source.read_data_chunks("2026-09-06", {"AAPL": date(2026, 9, 1)}))

    assert calls[0]["start"] == date(2026, 9, 2)
    assert calls[0]["end"] == date(2026, 9, 6)


def test_symbols_sharing_a_start_date_are_requested_together(source, universe):
    """Grouping by start is what keeps a daily increment to one request, not 503."""
    calls = universe(["AAPL", "MSFT"])

    list(
        source.read_data_chunks(
            "2026-09-06", {"AAPL": date(2026, 9, 1), "MSFT": date(2026, 9, 1)}
        )
    )

    assert len(calls) == 1
    assert calls[0]["tickers"] == ["AAPL", "MSFT"]


def test_a_symbol_already_current_is_not_requested_at_all(source, universe):
    calls = universe(["AAPL"])

    chunks = list(source.read_data_chunks("2026-09-06", {"AAPL": date(2026, 9, 6)}))

    assert calls == []
    assert chunks == []


def test_an_unwatermarked_symbol_starts_at_the_configured_history_floor(source, universe):
    calls = universe(["AAPL"])

    list(source.read_data_chunks("2026-09-06"))

    assert calls[0]["start"] == "2000-01-01"


def test_without_a_history_floor_the_window_is_the_last_week(monkeypatch, universe):
    monkeypatch.setattr(ohlcv, "get_sec_user_agent", lambda: "AURUM you@example.com")
    source = ohlcv.OHLCVDataSource({"name": "ohlcv"})
    calls = universe(["AAPL"])

    list(source.read_data_chunks("2026-09-06"))

    expected = (datetime.today() - timedelta(days=7)).strftime("%Y-%m-%d")
    assert calls[0]["start"] == expected


def test_the_interval_is_passed_through(source, universe):
    source.config["interval"] = "1m"
    calls = universe(["AAPL"])

    list(source.read_data_chunks("2026-09-06"))

    assert calls[0]["interval"] == "1m"
    assert calls[0]["auto_adjust"] is False


@pytest.mark.parametrize("empty", [None, pd.DataFrame()])
def test_an_empty_response_yields_nothing_rather_than_an_empty_frame(source, universe, empty):
    universe(["AAPL"], frame=empty)

    assert list(source.read_data_chunks("2026-09-06")) == []


def test_normalize_reshapes_the_multi_index_into_long_form():
    out = ohlcv.OHLCVDataSource._normalize(_raw(["AAPL", "MSFT"]))

    assert set(out.columns) == {"date", "symbol", "Close", "Volume"}
    assert sorted(out["symbol"].unique()) == ["AAPL", "MSFT"]
    assert len(out) == 4  # 2 dates x 2 symbols


def test_read_data_concatenates_every_chunk(source, universe):
    universe(["AAPL", "MSFT", "NVDA"])

    frame = source.read_data("2026-09-06")

    assert sorted(frame["symbol"].unique()) == ["AAPL", "MSFT", "NVDA"]


def test_read_data_returns_an_empty_frame_when_nothing_was_fetched(source, universe):
    universe(["AAPL"], frame=pd.DataFrame())

    assert source.read_data("2026-09-06").empty


def test_write_data_is_a_no_op_because_an_api_is_read_only(source):
    assert source.write_data("2026-09-06", pd.DataFrame({"a": [1]})) is None
