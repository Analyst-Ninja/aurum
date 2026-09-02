"""Environment loading for the ingestion framework.

Secrets live in the repo-root ``.env``; configs reference them by variable *name*.
"""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


@lru_cache(maxsize=1)
def load_env() -> None:
    """Load the repo-root ``.env`` once per process."""
    load_dotenv(ENV_PATH)


def get_sec_user_agent() -> str:
    """Return ``SEC_USER_AGENT``.

    SEC (10 req/s) and Wikipedia both reject requests without an honest
    User-Agent, so a missing value is a hard error rather than a silent 403.
    """
    load_env()
    user_agent = os.getenv("SEC_USER_AGENT")
    if not user_agent:
        raise ValueError(
            "SEC_USER_AGENT is not set — SEC and Wikipedia require an honest "
            "User-Agent, e.g. 'AURUM-Project you@example.com'"
        )
    return user_agent
