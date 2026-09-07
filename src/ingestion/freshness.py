"""Staleness gate: skip a feed whose landing table was loaded recently.

The EDGAR feeds are ``full_load: true`` with no watermark, so every run re-pulls the
entire history — ~1.9M rows and ~27 minutes across the six statement configs — against a
source that only changes when a company files. The semi-monthly schedule therefore does
the same full pull on the 1st and the 15th whether or not anything moved, and a rerun
after a partial failure repeats it again.

The gate compares the newest ``RUN_DATE`` already in the landing table against the run
date. Below ``min_refresh_gap_days`` the feed is fresh and the run is skipped; at or
above it, the feed loads as before. An absent or empty table is always stale — nothing
has been loaded, so there is nothing to be fresh.

Opt-in per config: a feed with no ``min_refresh_gap_days`` key is never gated, so the
market feeds are untouched.
"""

import logging
from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from src.ingestion.factory import factory

logger = logging.getLogger(__name__)

# Column BaseFeed._add_write_metadata stamps on every written row.
RUN_DATE_COLUMN = "RUN_DATE"
CONFIG_KEY = "min_refresh_gap_days"


def _coerce_date(value: Any) -> Optional[date]:
    """Parse whatever the landing table holds in RUN_DATE into a ``date``.

    The column is written from the CLI's ``--run_date`` string, so pandas types it as
    text; an older table may hold a real date. Anything unparseable is treated as
    "unknown", which makes the feed stale rather than silently skipped.
    """
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return pd.Timestamp(value).date()
    except (ValueError, TypeError):
        logger.warning("Could not parse %s value %r as a date", RUN_DATE_COLUMN, value)
        return None


def get_last_run_date(output_config: Dict[str, Any]) -> Optional[date]:
    """Newest ``RUN_DATE`` in the config's landing table, or ``None`` if there is none."""
    datasource = factory.create_datasource(output_config)
    try:
        raw = datasource.get_max_value(RUN_DATE_COLUMN)
    finally:
        disconnect = getattr(datasource, "disconnect", None)
        if callable(disconnect):
            disconnect()
    return _coerce_date(raw)


def check_freshness(
    config: Dict[str, Any], run_date: str
) -> Tuple[bool, Optional[int], Optional[int]]:
    """Decide whether a feed should run.

    Returns ``(should_run, gap_days, min_gap_days)``. ``min_gap_days`` is ``None`` when
    the config does not opt in, in which case ``should_run`` is always ``True``.
    ``gap_days`` is ``None`` when the landing table holds nothing to measure against.
    """
    min_gap = config.get(CONFIG_KEY)
    if min_gap is None:
        return True, None, None

    min_gap = int(min_gap)
    if min_gap <= 0:
        raise ValueError(f"{CONFIG_KEY} must be a positive number of days, got {min_gap}")

    output_config = config.get("output_datasource") or {}
    if not output_config:
        raise ValueError(f"{CONFIG_KEY} requires an output_datasource to measure against")

    last_run = get_last_run_date(output_config)
    if last_run is None:
        logger.info("No previous run recorded — treating feed as stale")
        return True, None, min_gap

    current = _coerce_date(run_date)
    if current is None:
        raise ValueError(f"Could not parse run_date {run_date!r}")

    gap = (current - last_run).days
    should_run = gap >= min_gap
    logger.info(
        "Last run %s, run date %s, gap %s day(s), threshold %s — %s",
        last_run,
        current,
        gap,
        min_gap,
        "running" if should_run else "skipping (data still fresh)",
    )
    return should_run, gap, min_gap
