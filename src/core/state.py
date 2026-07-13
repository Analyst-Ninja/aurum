"""Watermark state stores (spec §10)."""

import logging
from collections.abc import Callable
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


class LandingTableStateStore:
    """Watermarks derived from the landing table itself: MAX(date) per symbol.

    Day-one adapter (spec §10): no schema migration needed — writing data IS
    the watermark advance, so ``set_watermark`` is a no-op. A dedicated
    ``meta.watermarks`` store replaces this when EDGAR arrives.
    """

    def __init__(self, engine_factory: Callable[[], Any] | None, table: str):
        self._engine_factory = engine_factory
        self._table = table

    def get_watermarks(self, source: str) -> dict[str, date]:
        """Latest stored bar date per symbol; empty on first run / missing table."""
        if self._engine_factory is None:
            return {}
        try:
            df = pd.read_sql(
                f"SELECT symbol, MAX(date) AS max_date FROM {self._table} GROUP BY symbol",  # noqa: S608 — table name from config, not user input
                self._engine_factory(),
            )
        except (SQLAlchemyError, pd.errors.DatabaseError):
            logger.info("no watermarks for %s (first run or missing table)", source)
            return {}
        max_dates = pd.to_datetime(df["max_date"]).dt.date
        return dict(zip(df["symbol"], max_dates))

    def set_watermark(self, source: str, entity: str, value: date) -> None:
        """No-op: the landing table's MAX(date) already reflects written data."""
