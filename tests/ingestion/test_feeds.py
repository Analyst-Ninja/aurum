"""Every shipped feed's `process`.

`process` runs once per chunk, so it must stay row-wise — the assertions here are the
guard: uppercase every column, drop the reset-index artefact, change nothing else.
"""

import pandas as pd
import pytest

from src.ingestion.factory.registory import DATASOURCE_REGISTRY, FEED_REGISTRY
from src.ingestion.feed import financial_stmts_feed, stock_market  # noqa: F401 — registers


class _Stub:
    def __init__(self, config):
        self.config = config


MARKET_FEEDS = ["ohlcv_1d", "ohlcv_1min"]
STATEMENT_FEEDS = [
    "income_stmts_yearly",
    "income_stmts_quarterly",
    "cashflow_stmts_yearly",
    "cashflow_stmts_quarterly",
    "balance_sheet_stmts_yearly",
    "balance_sheet_stmts_quarterly",
]


@pytest.fixture
def build(monkeypatch):
    monkeypatch.setitem(DATASOURCE_REGISTRY, "stub", _Stub)

    def _build(feed_type):
        return FEED_REGISTRY[feed_type](
            {
                "type": feed_type,
                "feed_name": feed_type,
                "input_datasource": {"type": "stub"},
                "output_datasource": {"type": "stub"},
            }
        )

    return _build


@pytest.mark.parametrize("feed_type", MARKET_FEEDS + STATEMENT_FEEDS)
def test_every_feed_uppercases_every_column(build, feed_type):
    out = build(feed_type).process(pd.DataFrame({"date": ["2026-09-06"], "close": [1.0]}))

    assert list(out.columns) == ["DATE", "CLOSE"]


@pytest.mark.parametrize("feed_type", MARKET_FEEDS + STATEMENT_FEEDS)
def test_no_feed_adds_or_drops_rows(build, feed_type):
    frame = pd.DataFrame({"symbol": ["AAPL", "MSFT"], "value": [1.0, 2.0]})

    assert len(build(feed_type).process(frame)) == 2


@pytest.mark.parametrize("feed_type", STATEMENT_FEEDS)
def test_a_statement_feed_drops_the_reset_index_artefact(build, feed_type):
    """`_melt_statement` calls `reset_index()`, which can leave a bare `index` column."""
    frame = pd.DataFrame({"index": [0], "concept": ["Revenues"], "value": [1.0]})

    assert list(build(feed_type).process(frame).columns) == ["CONCEPT", "VALUE"]


@pytest.mark.parametrize("feed_type", STATEMENT_FEEDS)
def test_dropping_the_index_column_is_tolerant_of_its_absence(build, feed_type):
    frame = pd.DataFrame({"concept": ["Revenues"], "value": [1.0]})

    assert list(build(feed_type).process(frame).columns) == ["CONCEPT", "VALUE"]


@pytest.mark.parametrize("feed_type", MARKET_FEEDS)
def test_a_market_feed_keeps_a_column_called_index(build, feed_type):
    """Only the statement feeds melt, so only they have the artefact to drop."""
    frame = pd.DataFrame({"index": [0], "close": [1.0]})

    assert list(build(feed_type).process(frame).columns) == ["INDEX", "CLOSE"]
