"""The staleness gate: skip a full re-pull whose landing table is still fresh."""

import logging
from datetime import date, datetime

import pandas as pd
import pytest

from src.ingestion import freshness, truncate
from src.ingestion.feed.base_feed import BaseFeed


class _FakeSink:
    """Output datasource stub — only get_max_value/disconnect are exercised."""

    def __init__(self, max_value):
        self._max_value = max_value
        self.disconnected = False
        self.asked_for = None

    def get_max_value(self, column):
        self.asked_for = column
        return self._max_value

    def disconnect(self):
        self.disconnected = True


def _config(max_gap=10, **overrides):
    config = {
        "feed_name": "income_stmts_quarterly",
        "output_datasource": {
            "type": "postgres",
            "db_schema": "public",
            "table": "income_stmts_quarterly",
        },
    }
    if max_gap is not None:
        config["min_refresh_gap_days"] = max_gap
    config.update(overrides)
    return config


def _patch_sink(monkeypatch, max_value):
    sink = _FakeSink(max_value)
    monkeypatch.setattr(freshness.factory, "create_datasource", lambda _cfg: sink)
    return sink


@pytest.mark.parametrize(
    "last_run, run_date, expected_run, expected_gap",
    [
        ("2026-09-01", "2026-09-11", True, 10),   # exactly the threshold — runs
        ("2026-09-01", "2026-09-12", True, 11),   # past it — runs
        ("2026-09-01", "2026-09-10", False, 9),   # one day short — skips
        ("2026-09-01", "2026-09-01", False, 0),   # same day rerun — skips
    ],
)
def test_gap_decides_whether_the_feed_runs(
    monkeypatch, last_run, run_date, expected_run, expected_gap
):
    sink = _patch_sink(monkeypatch, last_run)

    should_run, gap, min_gap = freshness.check_freshness(_config(), run_date)

    assert (should_run, gap, min_gap) == (expected_run, expected_gap, 10)
    assert sink.asked_for == "RUN_DATE"
    assert sink.disconnected


def test_missing_config_key_never_gates(monkeypatch):
    def _fail(_config):
        raise AssertionError("must not touch the database when the key is absent")

    monkeypatch.setattr(freshness.factory, "create_datasource", _fail)

    assert freshness.check_freshness(_config(max_gap=None), "2026-09-07") == (True, None, None)


@pytest.mark.parametrize("max_value", [None, pd.NaT, float("nan")])
def test_empty_or_missing_table_is_always_stale(monkeypatch, max_value):
    _patch_sink(monkeypatch, max_value)

    assert freshness.check_freshness(_config(), "2026-09-07") == (True, None, 10)


@pytest.mark.parametrize(
    "stored", [date(2026, 9, 1), datetime(2026, 9, 1, 6, 30), "2026-09-01", pd.Timestamp("2026-09-01")]
)
def test_run_date_column_is_read_whatever_type_it_holds(monkeypatch, stored):
    _patch_sink(monkeypatch, stored)

    should_run, gap, _ = freshness.check_freshness(_config(), "2026-09-11")

    assert (should_run, gap) == (True, 10)


def test_unparseable_stored_value_is_treated_as_stale(monkeypatch, caplog):
    _patch_sink(monkeypatch, "not-a-date")

    with caplog.at_level(logging.WARNING):
        should_run, gap, _ = freshness.check_freshness(_config(), "2026-09-11")

    assert (should_run, gap) == (True, None)


def test_non_positive_threshold_is_rejected(monkeypatch):
    _patch_sink(monkeypatch, "2026-09-01")

    config = _config(max_gap=0)

    with pytest.raises(ValueError, match="positive number of days"):
        freshness.check_freshness(config, "2026-09-11")


def test_unparseable_run_date_is_rejected(monkeypatch):
    _patch_sink(monkeypatch, "2026-09-01")

    config = _config()

    with pytest.raises(ValueError, match="Could not parse run_date"):
        freshness.check_freshness(config, "yesterday")


class _Source:
    def __init__(self):
        self.reads = 0

    def read_data_chunks(self, run_date, watermarks=None):
        self.reads += 1
        yield pd.DataFrame({"SYMBOL": ["AAPL"]})


class _Feed(BaseFeed):
    def __init__(self, config, source):
        # Bypass BaseFeed.__init__, which builds datasources through the factory.
        self.config = config
        self.feed_name = "test"
        self.input_ds = source
        self.output_ds = None
        self.logger = logging.getLogger("Feed_test")
        self.start_time = datetime.now()
        self.metrics = {}

    def process(self, data):
        return data


def test_fresh_feed_skips_the_fetch_without_failing(monkeypatch):
    _patch_sink(monkeypatch, "2026-09-01")
    source = _Source()

    metrics = _Feed(_config(), source).run("2026-09-05")

    # A skip is a success — src/ingestion/cli.py only exits non-zero on FAILED.
    assert metrics["execution_status"] == "SKIPPED_FRESH"
    assert metrics["row_count"] == 0
    assert metrics["gap_days"] == 4
    assert metrics["min_refresh_gap_days"] == 10
    assert source.reads == 0


def test_stale_feed_still_fetches(monkeypatch):
    _patch_sink(monkeypatch, "2026-08-01")
    source = _Source()
    config = _config()
    config["output_datasource"]["cols_for_pk"] = ["SYMBOL"]
    config["output_datasource"]["primary_key"] = "MD5_HASH"

    writes = []

    class _Sink:
        def write_data(self, run_date, data):
            writes.append(data)

    feed = _Feed(config, source)
    feed.output_ds = _Sink()
    metrics = feed.run("2026-09-05")

    assert metrics["execution_status"] == "SUCCESS"
    assert source.reads == 1
    assert len(writes) == 1


def test_truncate_is_gated_too(monkeypatch):
    """Truncating a fresh table would reset the very value the gate measures."""
    _patch_sink(monkeypatch, "2026-09-01")
    monkeypatch.setattr(truncate, "read_config", lambda _path: _config())

    def _fail(self):
        raise AssertionError("must not connect when the data is still fresh")

    monkeypatch.setattr(truncate.PostgresDataSource, "connect", _fail)

    assert truncate.truncate_landing_table("cfg.yaml", "2026-09-05") is None
