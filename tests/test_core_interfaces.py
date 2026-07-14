"""FetchRequest is a frozen value object; end is exclusive by convention."""

from datetime import date

import pytest

from src.core.interfaces import FetchRequest


def test_fetch_request_frozen():
    req = FetchRequest(symbols=("AAPL",), start=date(2020, 1, 1), end=date(2020, 2, 1))
    with pytest.raises(Exception):
        req.start = date(2021, 1, 1)


def test_fetch_request_defaults_interval():
    req = FetchRequest(symbols=("AAPL",), start=date(2020, 1, 1), end=date(2020, 2, 1))
    assert req.interval == "1d"
