"""The universe fetchers.

Both endpoints 403 the default urllib User-Agent, so the honest one has to reach the
request — a missing `SEC_USER_AGENT` must fail here, loudly, rather than downstream as
an opaque HTTP error.
"""

import pytest
import requests

from src.utils import symbols


class _Response:
    def __init__(self, payload=None, text="", status=200):
        self._payload = payload
        self.text = text
        self.status = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status}")


@pytest.fixture
def captured(monkeypatch):
    """Record what was requested and hand back a canned response."""
    calls = {}

    def _get(url, headers=None, timeout=None):
        calls.update(url=url, headers=headers, timeout=timeout)
        return calls["response"]

    monkeypatch.setattr(symbols.requests, "get", _get)
    return calls


def test_sec_symbols_are_the_tickers_of_every_entry(captured):
    captured["response"] = _Response(
        payload={
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft"},
        }
    )

    assert symbols.get_sec_symbols("AURUM you@example.com") == ["AAPL", "MSFT"]


def test_sec_sends_the_honest_user_agent_and_the_timeout(captured):
    captured["response"] = _Response(payload={})

    symbols.get_sec_symbols("AURUM you@example.com", timeout=7)

    assert captured["url"] == symbols.SEC_TICKER_URL
    assert captured["headers"] == {"User-Agent": "AURUM you@example.com"}
    assert captured["timeout"] == 7


def test_an_http_error_from_sec_is_not_swallowed(captured):
    captured["response"] = _Response(payload={}, status=403)

    with pytest.raises(requests.HTTPError):
        symbols.get_sec_symbols("AURUM you@example.com")


_TABLE = """
<table>
 <tr><th>Symbol</th><th>Security</th></tr>
 <tr><td>AAPL</td><td>Apple</td></tr>
 <tr><td>BRK.B</td><td>Berkshire Hathaway</td></tr>
</table>
"""


def test_snp500_symbols_come_from_the_first_table(captured):
    captured["response"] = _Response(text=_TABLE)

    assert symbols.get_snp500_symbols("AURUM you@example.com") == ["AAPL", "BRK-B"]


def test_dotted_tickers_are_rewritten_the_way_yahoo_spells_them(captured):
    """Wikipedia writes BRK.B; Yahoo only answers to BRK-B."""
    captured["response"] = _Response(text=_TABLE)

    assert "BRK-B" in symbols.get_snp500_symbols("AURUM you@example.com")


def test_snp500_sends_the_honest_user_agent(captured):
    captured["response"] = _Response(text=_TABLE)

    symbols.get_snp500_symbols("AURUM you@example.com", timeout=3)

    assert captured["url"] == symbols.SNP500_TICKER_URL
    assert captured["headers"] == {"User-Agent": "AURUM you@example.com"}
    assert captured["timeout"] == 3


def test_an_http_error_from_wikipedia_is_not_swallowed(captured):
    captured["response"] = _Response(text="", status=403)

    with pytest.raises(requests.HTTPError):
        symbols.get_snp500_symbols("AURUM you@example.com")


@pytest.mark.parametrize("fetch", [symbols.get_sec_symbols, symbols.get_snp500_symbols])
@pytest.mark.parametrize("user_agent", [None, ""])
def test_a_missing_user_agent_fails_before_the_request(fetch, user_agent, monkeypatch):
    def _never(*args, **kwargs):  # pragma: no cover — the point is that it is not called
        raise AssertionError("the request must not be made without a User-Agent")

    monkeypatch.setattr(symbols.requests, "get", _never)

    with pytest.raises(Exception, match="SEC_USER_AGENT is not set"):
        fetch(user_agent)
