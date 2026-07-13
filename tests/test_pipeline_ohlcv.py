"""Incremental OHLCV pipeline behavior — ported from test_ohlcv_incremental.py."""

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from src.datasources.apis.yahoo.config import YahooConfig
from src.datasources.apis.yahoo.schemas import OhlcvBar
from src.pipelines import ohlcv_daily
from src.pipelines.ohlcv_daily import OhlcvDailyPipeline
from tests.fakes import InMemoryStateStore, ListSink

TODAY = datetime.now(timezone.utc).date()


class RecordingSource:
    """BatchDataSource fake: records FetchRequests, returns one schema frame."""

    schema = OhlcvBar

    def __init__(self):
        self.requests = []

    def fetch(self, request):
        self.requests.append(request)
        n = len(request.symbols)
        frame = pd.DataFrame(
            {"date": [request.start] * n, "symbol": list(request.symbols)}
        )
        for col in OhlcvBar.frame_columns()[2:]:
            frame[col] = 0.0
        yield frame


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(ohlcv_daily.time, "sleep", lambda _s: None)
    source, sink = RecordingSource(), ListSink()

    def build(watermarks=None):
        return (
            OhlcvDailyPipeline(
                source=source,
                state=InMemoryStateStore(watermarks),
                sink=sink,
                config=YahooConfig(),
            ),
            source,
            sink,
        )

    return build


def _starts(source):
    return [(req.start, list(req.symbols)) for req in source.requests]


def test_new_symbol_fetched_from_history_floor(env):
    pipeline, source, _ = env()
    pipeline.run(["AAPL"])
    assert _starts(source) == [(date(2000, 1, 1), ["AAPL"])]


def test_known_symbol_fetched_from_watermark_plus_one(env):
    wm = TODAY - timedelta(days=5)
    pipeline, source, _ = env({"AAPL": wm})
    pipeline.run(["AAPL"])
    assert _starts(source) == [(wm + timedelta(days=1), ["AAPL"])]


def test_up_to_date_symbol_not_fetched(env):
    pipeline, source, sink = env({"AAPL": TODAY})
    pipeline.run(["AAPL"])
    assert source.requests == []
    assert sink.written == []


def test_symbol_current_through_yesterday_not_fetched(env):
    # end is exclusive: watermark = yesterday means nothing new today
    pipeline, source, _ = env({"AAPL": TODAY - timedelta(days=1)})
    pipeline.run(["AAPL"])
    assert source.requests == []


def test_different_starts_grouped_separately(env):
    wm = TODAY - timedelta(days=5)
    pipeline, source, _ = env({"OLD": wm})
    pipeline.run(["NEW", "OLD"])
    starts = dict(_starts(source))
    assert starts[date(2000, 1, 1)] == ["NEW"]
    assert starts[wm + timedelta(days=1)] == ["OLD"]


def test_written_frames_stamped_with_run_metadata(env):
    pipeline, _, sink = env()
    pipeline.run(["AAPL"])
    assert len(sink.written) == 1
    frame = sink.written[0]
    assert set(frame["run_date"]) == {TODAY}
    assert frame["inserted_at"].notna().all()


def test_run_returns_rows_written(env):
    pipeline, _, _ = env()
    assert pipeline.run(["AAPL", "MSFT"]) == 2


def test_empty_fetch_writes_nothing(env, monkeypatch):
    pipeline, source, sink = env()
    monkeypatch.setattr(source, "fetch", lambda request: iter(()))
    pipeline.run(["AAPL"])
    assert sink.written == []


def test_sleep_between_writes(env, monkeypatch):
    slept = []
    monkeypatch.setattr(ohlcv_daily.time, "sleep", lambda s: slept.append(s))
    pipeline, _, _ = env()
    pipeline.run(["AAPL"])
    assert slept == [10]  # YahooConfig default sleep_seconds
