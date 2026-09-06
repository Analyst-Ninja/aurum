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
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.ingestion.datasources.storage.db import PostgresDataSource
from src.utils.config_reader import read_config

logger = logging.getLogger(__name__)


def _validate_identifier(identifier: str) -> str:
    """Reject anything that cannot be a bare SQL identifier.

    Schema and table names cannot be bound as query parameters, so they are
    interpolated — same constraint ``PostgresDataSource.get_watermarks`` works under.
    """
    if not identifier or not identifier.replace("_", "").isalnum():
        raise ValueError(f"Invalid SQL identifier: {identifier!r}")
    return identifier


def truncate_landing_table(config_path: str) -> str:
    """Truncate the table the config's output datasource writes to.

    Returns the qualified table name. A table that does not exist yet is a no-op —
    the first run creates it on write.
    """
    config = read_config(Path(config_path))
    output = config.get("output_datasource") or {}

    if output.get("type") != "postgres":
        raise ValueError(
            f"{config_path} writes to {output.get('type')!r}, not postgres — nothing to truncate"
        )

    schema = _validate_identifier(output.get("db_schema", "public"))
    table = _validate_identifier(output.get("table", ""))
    qualified = f"{schema}.{table}"

    datasource = PostgresDataSource(output)
    datasource.connect()
    try:
        with datasource.conn.begin() as connection:
            connection.execute(text(f'TRUNCATE TABLE "{schema}"."{table}"'))
    except SQLAlchemyError as error:
        if _is_missing_relation(error):
            logger.info("Landing table %s does not exist yet — nothing to truncate", qualified)
            return qualified
        raise
    finally:
        datasource.disconnect()

    logger.info("Truncated %s", qualified)
    return qualified


def _is_missing_relation(error: BaseException) -> bool:
    """Walk the driver exception chain looking for Postgres' undefined_table."""
    codes = set()
    messages = []
    while error is not None:
        codes.add(getattr(error, "pgcode", None))
        messages.append(str(error).lower())
        error = getattr(error, "orig", None)

    joined = " ".join(messages)
    return "42P01" in codes or "undefinedtable" in joined or "does not exist" in joined


def main():
    parser = argparse.ArgumentParser(description="Truncate a feed's landing table")
    parser.add_argument("--config", "-c", required=True, help="Path to feed config file")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    # Exit non-zero on failure for the same reason src/ingestion/cli.py does: the
    # `&&` chain in the ECS task command and Step Functions' runTask.sync both key on
    # the exit code, and a silent failure here means loading into a stale table.
    try:
        truncate_landing_table(args.config)
    except (SQLAlchemyError, ValueError, OSError) as error:
        logging.error("Truncate failed for %s: %s", args.config, error)
        sys.exit(1)


if __name__ == "__main__":
    main()
