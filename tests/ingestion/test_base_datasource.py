"""The read_data_chunks default contract.

BaseFeed.run iterates read_data_chunks, so a datasource that implements only read_data —
EDGAR's FinancialStmtsDatasource, and anything added later — has to keep working with no
change of its own. These tests pin that down.
"""

import pandas as pd
import pytest

from src.ingestion.datasources.api.edgar.financial_stmts import FinancialStmtsDatasource
from src.ingestion.datasources.base_datasource import BaseDatasource


class _ReadOnlySource(BaseDatasource):
    """Implements the two abstract methods and nothing else — the minimum contract."""

    def __init__(self, frame):
        super().__init__({})
        self._frame = frame
        self.calls = []

    def read_data(self, run_date, watermarks=None):
        self.calls.append((run_date, watermarks))
        return self._frame

    def write_data(self, run_date, data):
        raise NotImplementedError


def test_default_yields_the_single_frame_read_data_returns():
    frame = pd.DataFrame({"SYMBOL": ["AAPL", "MSFT"]})
    source = _ReadOnlySource(frame)

    chunks = list(source.read_data_chunks("2026-09-06", watermarks={"AAPL": "2026-09-01"}))

    assert len(chunks) == 1
    pd.testing.assert_frame_equal(chunks[0], frame)
    assert source.calls == [("2026-09-06", {"AAPL": "2026-09-01"})]


@pytest.mark.parametrize("empty", [pd.DataFrame(), None])
def test_an_empty_read_yields_no_chunks(empty):
    """So BaseFeed can treat "no chunks" as SUCCESS_NO_DATA."""
    assert list(_ReadOnlySource(empty).read_data_chunks("2026-09-06")) == []


def test_edgar_uses_the_default_and_its_read_data_still_binds(monkeypatch):
    """EDGAR declares read_data(run_date, watermarks) positionally; the default calls it
    by keyword. Confirms that binds, and that EDGAR adds no override of its own."""
    assert FinancialStmtsDatasource.read_data_chunks is BaseDatasource.read_data_chunks

    monkeypatch.setenv("SEC_USER_AGENT", "AURUM-Test test@example.com")
    source = FinancialStmtsDatasource({})

    frame = pd.DataFrame({"SYMBOL": ["AAPL"]})
    monkeypatch.setattr(
        FinancialStmtsDatasource, "read_data", lambda self, run_date, watermarks: frame
    )

    assert [len(c) for c in source.read_data_chunks("2026-09-06", watermarks={})] == [1]
