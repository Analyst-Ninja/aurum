"""PostgresSink appends frames via to_sql, preserving current write behavior."""

import pandas as pd
import pytest

from src.core.errors import PermanentError
from src.core.sinks import PostgresSink


def test_write_appends_to_table(monkeypatch):
    captured = {}

    def fake_to_sql(self, name, con, if_exists, index):
        captured.update(name=name, if_exists=if_exists, index=index, rows=len(self))

    monkeypatch.setattr(pd.DataFrame, "to_sql", fake_to_sql)
    sink = PostgresSink(engine_factory=lambda: object())
    df = pd.DataFrame({"symbol": ["AAPL", "MSFT"]})

    written = sink.write(df, table="ohlcv_1d", natural_key=["date", "symbol"])

    assert written == 2
    assert captured == {
        "name": "ohlcv_1d",
        "if_exists": "append",
        "index": False,
        "rows": 2,
    }


def test_missing_engine_raises_permanent():
    sink = PostgresSink(engine_factory=None)
    with pytest.raises(PermanentError, match="POSTGRES_URL"):
        sink.write(pd.DataFrame({"a": [1]}), table="t", natural_key=["a"])
