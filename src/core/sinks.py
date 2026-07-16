"""Sinks: idempotency-oriented writers (spec §4). Postgres only for now."""

import logging
from collections.abc import Callable
from typing import Any

import pandas as pd

from src.core.errors import PermanentError

logger = logging.getLogger(__name__)


class PostgresSink:
    """Appends validated frames to a landing table.

    NOTE: true ``ON CONFLICT DO NOTHING`` upsert on ``natural_key`` requires a
    unique constraint on the landing table; that lands with the landing DDL
    (infra Phase 0). Until then this preserves today's append behavior — the
    watermark logic upstream already prevents duplicate windows.
    """

    def __init__(self, engine_factory: Callable[[], Any] | None):
        self._engine_factory = engine_factory

    def write(self, df: pd.DataFrame, *, table: str, natural_key: list[str]) -> int:
        if self._engine_factory is None:
            raise PermanentError("POSTGRES_URL is not configured")
        df.to_sql(
            name=table, con=self._engine_factory(), if_exists="append", index=False
        )
        logger.info("wrote %d rows to %s", len(df), table)
        return len(df)
