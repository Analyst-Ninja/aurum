"""SEC company_tickers.json symbol fetch: parsing + mandatory honest UA."""

import pytest

from src.datasources.apis.yahoo import symbols as symbols_mod
from src.datasources.apis.yahoo.symbols import get_sec_symbols


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
        }


def test_parses_tickers(monkeypatch):
    captured = {}

    def fake_get(url, headers, timeout):
        captured.update(headers=headers)
        return FakeResponse()

    monkeypatch.setattr(symbols_mod.requests, "get", fake_get)
    assert get_sec_symbols("AURUM-Project real@person.dev") == ["AAPL", "MSFT"]
    assert captured["headers"]["User-Agent"] == "AURUM-Project real@person.dev"


def test_missing_user_agent_raises():
    with pytest.raises(Exception, match="SEC_USER_AGENT"):
        get_sec_symbols(None)
