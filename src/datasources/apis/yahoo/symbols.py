"""Symbol universe from SEC company_tickers.json (spec §14: universe from config)."""

import os

import requests

from src.core.errors import PermanentError

_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"


def get_sec_symbols(timeout: int = 100) -> list[str]:
    """All tickers from SEC's company_tickers.json.

    SEC requires an honest ``User-Agent`` (403 otherwise) — set SEC_USER_AGENT,
    e.g. ``AURUM-Project you@example.com``.
    """
    user_agent = os.getenv("SEC_USER_AGENT")
    if not user_agent:
        raise PermanentError(
            "SEC_USER_AGENT is not set — SEC requires an honest User-Agent"
        )
    response = requests.get(
        _TICKER_URL, headers={"User-Agent": user_agent}, timeout=timeout
    )
    response.raise_for_status()
    return [item["ticker"] for item in response.json().values()]
