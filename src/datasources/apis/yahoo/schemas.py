"""Record schema for Yahoo OHLCV daily bars (landing table ohlcv_1d)."""

import datetime as dt
from typing import ClassVar

from pydantic import Field

from src.core.schemas import BaseRecord


class OhlcvBar(BaseRecord):
    """One row per symbol per day, raw Yahoo column names preserved."""

    table: ClassVar[str] = "ohlcv_1d"
    natural_key: ClassVar[list[str]] = ["date", "symbol"]

    date: dt.date
    symbol: str
    adj_close: float = Field(alias="Adj Close")
    close: float = Field(alias="Close")
    dividends: float = Field(alias="Dividends")
    high: float = Field(alias="High")
    low: float = Field(alias="Low")
    open: float = Field(alias="Open")
    stock_splits: float = Field(alias="Stock Splits")
    volume: float = Field(alias="Volume")
