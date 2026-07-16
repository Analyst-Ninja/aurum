"""Symbol universe from SEC and S&P 500 index company_tickers.json (spec §14: universe from config)."""

from io import StringIO

import requests
import pandas as pd

from src.core.errors import PermanentError

SEC_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SNP500_TICKER_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def get_sec_symbols(user_agent: str | None, timeout: int = 100) -> list[str]:
    """All tickers from SEC's company_tickers.json.

    SEC requires an honest ``User-Agent`` (403 otherwise) — set SEC_USER_AGENT
    in ``.env`` (loaded via CoreConfig), e.g. ``AURUM-Project you@example.com``.
    """
    if not user_agent:
        raise PermanentError(
            "SEC_USER_AGENT is not set — SEC requires an honest User-Agent"
        )
    response = requests.get(
        SEC_TICKER_URL, headers={"User-Agent": user_agent}, timeout=timeout
    )
    response.raise_for_status()
    return [item["ticker"] for item in response.json().values()]


def get_snp500_symbols(user_agent: str | None, timeout: int = 100) -> list[str]:
    """S&P 500 ticker symbols from the Wikipedia constituents table.

    Wikipedia returns 403 for the default urllib User-Agent, so fetch with an
    honest ``User-Agent`` (same convention as SEC) and hand the HTML to pandas.
    """
    if not user_agent:
        raise PermanentError(
            "SEC_USER_AGENT is not set — Wikipedia rejects the default User-Agent"
        )
    response = requests.get(
        SNP500_TICKER_URL, headers={"User-Agent": user_agent}, timeout=timeout
    )
    response.raise_for_status()

    # The first table contains the company list
    tables = pd.read_html(StringIO(response.text))
    df = tables[0]

    # Extract the 'Symbol' column and convert it to a Python list
    return df["Symbol"].tolist()
