import pandas as pd
import pytest

from src.ingestion.feed.base_feed import BaseFeed


class _Source:
    """Stands in for a paging datasource; records what the feed asked for."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.calls = []

    def read_data(self, run_date, watermarks=None):
        return pd.concat(self._chunks, ignore_index=True) if self._chunks else pd.DataFrame()

    def read_data_chunks(self, run_date, watermarks=None):
        self.calls.append(watermarks)
        yield from self._chunks

    def get_watermarks(self, group_by="symbol", date_column="date"):
        return {"AAPL": "2026-09-01"}


class _Sink:
    def __init__(self):
        self.writes = []

    def write_data(self, run_date, data):
        self.writes.append(data)

    def get_watermarks(self, group_by="symbol", date_column="date"):
        return {"AAPL": "2026-09-01"}


class _Feed(BaseFeed):
    def __init__(self, source, sink):
        # Bypass BaseFeed.__init__, which builds datasources through the factory.
        self.config = {"output_datasource": {"cols_for_pk": ["SYMBOL"], "primary_key": "MD5_HASH"}}
        self.feed_name = "test"
        self.input_ds = source
        self.output_ds = sink
        import logging
        from datetime import datetime

        self.logger = logging.getLogger("Feed_test")
        self.start_time = datetime.now()
        self.metrics = {}
        self.processed = 0

    def process(self, data):
        self.processed += 1
        return data


def _chunk(*symbols):
    return pd.DataFrame({"SYMBOL": list(symbols)})


@pytest.fixture
def feed_parts():
    def build(chunks):
        source, sink = _Source(chunks), _Sink()
        return _Feed(source, sink), source, sink

    return build


def test_each_chunk_is_written_before_the_next_is_read(feed_parts):
    """The point of the change: peak memory is one batch, not the whole pull."""
    feed, _, sink = feed_parts([_chunk("AAPL"), _chunk("MSFT", "GOOG")])

    metrics = feed.run("2026-09-06", full_load=True)

    assert metrics["execution_status"] == "SUCCESS"
    assert metrics["row_count"] == 3
    assert [len(frame) for frame in sink.writes] == [1, 2]  # two writes, not one concat
    assert feed.processed == 2  # process() runs per chunk


def test_no_chunks_is_success_no_data(feed_parts):
    """A market holiday returns nothing and must stay green."""
    feed, _, sink = feed_parts([])

    metrics = feed.run("2026-09-06", full_load=True)

    assert metrics["execution_status"] == "SUCCESS_NO_DATA"
    assert metrics["row_count"] == 0
    assert sink.writes == []


def test_write_metadata_is_added_to_every_chunk(feed_parts):
    feed, _, sink = feed_parts([_chunk("AAPL"), _chunk("MSFT")])

    feed.run("2026-09-06", full_load=True)

    for frame in sink.writes:
        assert {"RUN_DATE", "EXECUTION_ID", "MD5_HASH"} <= set(frame.columns)


def test_incremental_passes_watermarks_to_the_chunked_read(feed_parts):
    feed, source, _ = feed_parts([_chunk("AAPL")])

    feed.run("2026-09-06", full_load=False)

    assert source.calls == [{"AAPL": "2026-09-01"}]


def test_a_failure_mid_stream_is_reported_not_raised(feed_parts):
    """BaseFeed.run swallows exceptions; only the metrics dict carries the failure."""
    feed, _, sink = feed_parts([_chunk("AAPL"), _chunk("MSFT")])

    def _explode(run_date, data):
        sink.writes.append(data)
        if len(sink.writes) == 2:
            raise RuntimeError("connection reset")

    sink.write_data = _explode

    metrics = feed.run("2026-09-06", full_load=True)

    assert metrics["execution_status"] == "FAILED"
    assert "connection reset" in metrics["error_message"]
