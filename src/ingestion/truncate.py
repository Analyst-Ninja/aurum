"""Empty a feed's landing table before a full reload.

The EDGAR feeds are ``full_load: true`` and carry no watermark columns, so every run
re-pulls the entire history. ``Database.write_data`` appends with
``to_sql(if_exists="append")`` and there is no unique index on ``MD5_HASH``, so a second
run duplicates every row it already loaded — ~1.9M of them, growing linearly with the
schedule.

Truncating first makes the landing table match the contract the config already declares:
one full snapshot per run. Bronze deduplicates downstream either way, but the landing
tables should not need it to.

Run it per config, immediately before that config's load, so a failure halfway through a
six-config batch leaves one table empty rather than all six:

    python -m src.ingestion.truncate -c src/ingestion/configs/edgar/income_statements_quarterly.yaml
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.ingestion.datasources.storage.db import (
    PostgresDataSource,
    is_missing_relation,
    validate_identifier,
)
from src.ingestion.freshness import check_freshness
from src.utils.config_reader import read_config

logger = logging.getLogger(__name__)


def truncate_landing_table(config_path: str, run_date: str | None = None) -> str | None:
    """Truncate the table the config's output datasource writes to.

    Returns the qualified table name, or ``None`` when the config's staleness gate
    (``min_refresh_gap_days``) says the data is still fresh. A table that does not exist
    yet is a no-op — the first run creates it on write.

    The gate has to be checked HERE as well as in the feed, and checked first: truncating
    empties the table the feed measures freshness against, so a truncate that ran anyway
    would make every subsequent run look stale and the gate would never fire.
    """
    config = read_config(Path(config_path))
    output = config.get("output_datasource") or {}

    run_date = run_date or datetime.now().strftime("%Y-%m-%d")
    should_run, gap_days, min_gap_days = check_freshness(config, run_date)
    if not should_run:
        logger.info(
            "Skipping truncate for %s — last load was %s day(s) ago, below the %s-day refresh gap",
            config_path,
            gap_days,
            min_gap_days,
        )
        return None

    if output.get("type") != "postgres":
        raise ValueError(
            f"{config_path} writes to {output.get('type')!r}, not postgres — nothing to truncate"
        )

    schema = validate_identifier(output.get("db_schema", "public"))
    table = validate_identifier(output.get("table", ""))
    qualified = f"{schema}.{table}"

    datasource = PostgresDataSource(output)
    datasource.connect()
    try:
        with datasource.conn.begin() as connection:
            connection.execute(text(f'TRUNCATE TABLE "{schema}"."{table}"'))
    except SQLAlchemyError as error:
        if is_missing_relation(error):
            logger.info("Landing table %s does not exist yet — nothing to truncate", qualified)
            return qualified
        raise
    finally:
        datasource.disconnect()

    logger.info("Truncated %s", qualified)
    return qualified


def main():
    parser = argparse.ArgumentParser(description="Truncate a feed's landing table")
    parser.add_argument("--config", "-c", required=True, help="Path to feed config file")
    parser.add_argument(
        "--run_date",
        "-d",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="Run date the staleness gate measures against (default: today)",
    )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    # Exit non-zero on failure for the same reason src/ingestion/cli.py does: the
    # `&&` chain in the ECS task command and Step Functions' runTask.sync both key on
    # the exit code, and a silent failure here means loading into a stale table.
    try:
        truncate_landing_table(args.config, args.run_date)
    except (SQLAlchemyError, ValueError, OSError) as error:
        logging.exception("Truncate failed for %s: %s", args.config, error)
        sys.exit(1)


if __name__ == "__main__":
    main()
