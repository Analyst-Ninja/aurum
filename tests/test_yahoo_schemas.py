"""OhlcvBar mirrors the existing ohlcv_1d landing columns exactly."""

from src.datasources.apis.yahoo.schemas import OhlcvBar


def test_table_and_natural_key():
    assert OhlcvBar.table == "ohlcv_1d"
    assert OhlcvBar.natural_key == ["date", "symbol"]


def test_frame_columns_match_landing_table():
    assert OhlcvBar.frame_columns() == [
        "date",
        "symbol",
        "Adj Close",
        "Close",
        "Dividends",
        "High",
        "Low",
        "Open",
        "Stock Splits",
        "Volume",
    ]
