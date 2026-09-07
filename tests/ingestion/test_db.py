import pandas as pd
import pytest

from src.ingestion.datasources.storage import db


class _Sink(db.Database):
    """Database is abstract; write_data is the only thing under test here."""

    def get_watermarks(self, group_by="symbol", date_column="date"):
        raise NotImplementedError

    def get_max_value(self, column):
        raise NotImplementedError

    def connect(self):
        self.conn = object()

    def disconnect(self):
        self.conn = None


@pytest.fixture
def sink():
    return _Sink({"table": "ohlcv_1d", "db_schema": "public"})


def test_write_data_chunks_the_insert(sink, monkeypatch):
    """A first load with no watermark is the whole history in one frame; without a
    chunksize pandas materialises the entire parameter list and the task is OOM-killed."""
    captured = {}
    monkeypatch.setattr(
        pd.DataFrame, "to_sql", lambda self, **kwargs: captured.update(kwargs)
    )

    sink.write_data("2026-09-06", pd.DataFrame({"SYMBOL": ["AAPL"], "CLOSE": [1.0]}))

    assert captured["chunksize"] == db.WRITE_CHUNK_ROWS
    assert captured["name"] == "ohlcv_1d"
    assert captured["if_exists"] == "append"
    assert captured["index"] is False


def test_write_data_releases_the_engine(sink, monkeypatch):
    monkeypatch.setattr(pd.DataFrame, "to_sql", lambda self, **kwargs: None)

    sink.write_data("2026-09-06", pd.DataFrame({"SYMBOL": ["AAPL"]}))

    assert sink.conn is None
