"""BaseRecord: frame validation is cheap (columns, not rows) and alias-aware."""

from typing import ClassVar

import pandas as pd
import pytest
from pydantic import Field

from src.core.errors import SchemaMismatchError
from src.core.schemas import BaseRecord


class Toy(BaseRecord):
    table: ClassVar[str] = "toy"
    natural_key: ClassVar[list[str]] = ["symbol"]

    symbol: str
    adj_close: float = Field(alias="Adj Close")


def test_frame_columns_use_aliases():
    assert Toy.frame_columns() == ["symbol", "Adj Close"]


def test_validate_frame_returns_schema_columns_in_order():
    df = pd.DataFrame({"extra": [1], "Adj Close": [2.0], "symbol": ["AAPL"]})
    out = Toy.validate_frame(df)
    assert list(out.columns) == ["symbol", "Adj Close"]


def test_validate_frame_missing_column_raises():
    df = pd.DataFrame({"symbol": ["AAPL"]})
    with pytest.raises(SchemaMismatchError, match="Adj Close"):
        Toy.validate_frame(df)


def test_records_are_frozen():
    row = Toy(symbol="AAPL", **{"Adj Close": 1.0})
    with pytest.raises(Exception):
        row.symbol = "MSFT"
