"""`run_feed` is thin, and every line of it is a contract the CLI depends on.

It must return the feed's metrics dict unchanged (the CLI reads `execution_status`
off it to decide the exit code), and it must refuse anything that is not a `BaseFeed`
rather than calling `run` on it and failing somewhere further down.
"""

import pandas as pd
import pytest

from src.ingestion import runner
from src.ingestion.factory.registory import DATASOURCE_REGISTRY
from src.ingestion.feed.base_feed import BaseFeed


class _Stub:
    """Stands in for both datasources — BaseFeed builds them in its constructor."""

    def __init__(self, config):
        self.config = config


class _Feed(BaseFeed):
    def __init__(self, config):
        super().__init__(config)
        self.calls = []

    def process(self, data: pd.DataFrame) -> pd.DataFrame:  # pragma: no cover
        return data

    def run(self, run_date, full_load=True):
        self.calls.append((run_date, full_load))
        return {"execution_status": "SUCCESS", "row_count": 7}


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setitem(DATASOURCE_REGISTRY, "stub", _Stub)
    return {
        "type": "ohlcv_1d",
        "feed_name": "ohlcv",
        "input_datasource": {"type": "stub"},
        "output_datasource": {"type": "stub"},
    }


def _patch(monkeypatch, config, feed):
    monkeypatch.setattr(runner, "read_config", lambda path: config)
    monkeypatch.setattr(runner.factory, "create_feed", lambda cfg: feed)


def test_metrics_are_returned_untouched(monkeypatch, config):
    feed = _Feed(config)
    _patch(monkeypatch, config, feed)

    metrics = runner.run_feed("configs/yahoo/ohlcv_1d.yaml", "2026-09-06")

    assert metrics == {"execution_status": "SUCCESS", "row_count": 7}


def test_run_date_and_full_load_reach_the_feed(monkeypatch, config):
    feed = _Feed(config)
    _patch(monkeypatch, config, feed)

    runner.run_feed("configs/yahoo/ohlcv_1d.yaml", "2026-09-06", full_load=False)

    assert feed.calls == [("2026-09-06", False)]


def test_full_load_defaults_to_true(monkeypatch, config):
    """`-f` defaults to True at the CLI too; the two must not disagree."""
    feed = _Feed(config)
    _patch(monkeypatch, config, feed)

    runner.run_feed("configs/yahoo/ohlcv_1d.yaml", "2026-09-06")

    assert feed.calls == [("2026-09-06", True)]


def test_a_none_metrics_dict_becomes_an_empty_one(monkeypatch, config):
    """The CLI does `.get("execution_status")` on the result; None would raise there."""
    feed = _Feed(config)
    feed.run = lambda run_date, full_load=True: None
    _patch(monkeypatch, config, feed)

    assert runner.run_feed("c.yaml", "2026-09-06") == {}


def test_a_non_feed_is_rejected_before_it_is_run(monkeypatch, config):
    _patch(monkeypatch, config, object())

    with pytest.raises(TypeError, match="object is not supported"):
        runner.run_feed("c.yaml", "2026-09-06")


def test_the_config_path_is_read_as_a_path(monkeypatch, config):
    seen = {}

    def _read(path):
        seen["path"] = path
        return config

    monkeypatch.setattr(runner, "read_config", _read)
    monkeypatch.setattr(runner.factory, "create_feed", _Feed)

    runner.run_feed("src/ingestion/configs/yahoo/ohlcv_1d.yaml", "2026-09-06")

    assert seen["path"].name == "ohlcv_1d.yaml"
