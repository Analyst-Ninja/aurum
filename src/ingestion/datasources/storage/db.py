import os
from abc import abstractmethod
from datetime import date
from typing import Dict, Any

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from src.ingestion.datasources.base_datasource import BaseDatasource
from src.ingestion.factory.registory import register_datasource
from src.utils.env import load_env

load_env()

# Rows per INSERT batch in write_data.
#
# Without it, pandas builds the parameter list for the WHOLE frame before sending
# anything, so peak memory tracks row count rather than staying flat. That is fine for a
# daily increment and fatal for a first load: with no watermark to resume from, a feed
# pulls 503 symbols from 2000 to today in one frame, and the ingest task was OOM-killed
# (exit 137, "OutOfMemoryError: container killed due to memory usage") writing it on
# 8 GB — 2026-09-06, against a freshly rebuilt RDS with no landing tables.
#
# Chunking bounds the batch instead of the frame. It does not make the write faster and
# is not meant to; pandas still issues one executemany per chunk.
WRITE_CHUNK_ROWS = 50_000


def is_missing_relation(error: BaseException | None) -> bool:
    """Walk a driver exception chain looking for Postgres' undefined_table (42P01).

    A landing table that does not exist yet is normal on the first run: the write path
    creates it. Watermark, freshness and truncate lookups all have to tell that apart
    from a real database error, so the detection lives here once.
    """
    codes = set()
    messages = []
    while error is not None:
        codes.add(getattr(error, "pgcode", None))
        messages.append(str(error).lower())
        error = getattr(error, "orig", None)

    joined = " ".join(messages)
    return "42P01" in codes or "undefinedtable" in joined or "does not exist" in joined


def validate_identifier(identifier: str) -> str:
    """Reject anything that cannot be interpolated as a bare SQL identifier.

    Schema, table and column names cannot be bound as query parameters.
    """
    if not identifier or not identifier.replace("_", "").isalnum():
        raise ValueError(f"Invalid SQL identifier: {identifier!r}")
    return identifier



class Database(BaseDatasource):

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.conn = None

    def read_data(self) -> pd.DataFrame:
        pass

    def write_data(self, run_date: str, data: pd.DataFrame) -> None:
        self.connect()
        self.logger.info(f"Writing data to {run_date} ({len(data)} rows)")
        data.to_sql(
            name=self.config.get("table", ""),
            con=self.conn,
            if_exists='append',
            index=False,
            chunksize=WRITE_CHUNK_ROWS,
        )

        self.logger.info(f"Wrote data to {run_date}")
        self.disconnect()

    @abstractmethod
    def get_max_value(self, column: str):
        pass

    @abstractmethod
    def get_watermarks(self, group_by: str = "symbol", date_column: str = "date") -> Dict[str, date]:
        pass

    @abstractmethod
    def connect(self):
        pass

    @abstractmethod
    def disconnect(self):
        pass

@register_datasource("postgres")
class PostgresDataSource(Database):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

    def get_watermarks(self, group_by: str = "symbol", date_column: str = "date") -> Dict[str, date]:
        """Return the latest stored date for each value in ``group_by``."""
        table = self.config.get("table")
        schema = self.config.get("db_schema", "public")
        if not table:
            raise ValueError("Datasource must include a table for watermark lookup")

        # Identifiers cannot be bound as SQL parameters, so validate them before
        # interpolating them into the query.
        for identifier in (schema, table, group_by, date_column):
            validate_identifier(identifier)

        self.connect()
        query = text(
            f'SELECT "{group_by}" AS group_key, '
            f'MAX("{date_column}") AS max_date '
            f'FROM "{schema}"."{table}" '
            f'GROUP BY "{group_by}"'
        )

        try:
            frame = pd.read_sql_query(query, self.conn)
        except (SQLAlchemyError, pd.errors.DatabaseError) as error:
            # A missing landing table is normal on the first run; the write
            # path will create it after the initial full load.
            if is_missing_relation(error):
                self.logger.info("Watermark table %s.%s does not exist yet", schema, table)
                return {}
            raise

        if frame.empty:
            return {}

        return {
            row.group_key: pd.Timestamp(row.max_date).date()
            for row in frame.itertuples(index=False)
            if pd.notna(row.group_key) and pd.notna(row.max_date)
        }

    def get_max_value(self, column: str):
        """Return ``MAX(column)`` from the configured table, or ``None``.

        ``None`` covers both "the table does not exist yet" and "it exists but is
        empty" — for a freshness check the two mean the same thing: nothing has been
        loaded, so the caller must load.
        """
        table = self.config.get("table")
        schema = self.config.get("db_schema", "public")
        if not table:
            raise ValueError("Datasource must include a table for max-value lookup")

        for identifier in (schema, table, column):
            validate_identifier(identifier)

        self.connect()
        query = text(f'SELECT MAX("{column}") AS max_value FROM "{schema}"."{table}"')

        try:
            frame = pd.read_sql_query(query, self.conn)
        except (SQLAlchemyError, pd.errors.DatabaseError) as error:
            if is_missing_relation(error):
                self.logger.info("Table %s.%s does not exist yet", schema, table)
                return None
            raise

        if frame.empty:
            return None

        value = frame["max_value"].iloc[0]
        return None if pd.isna(value) else value

    def connect(self):
        user = os.getenv(self.config.get("username", ""))
        password = os.getenv(self.config.get("password", ""))
        host = os.getenv(self.config.get("host", ""))
        port = os.getenv(self.config.get("port", ""))
        db_name = self.config.get("db_name", "")

        connection_string = f"postgresql://{user}:{password}@{host}:{port}/{db_name}"
        self.conn = create_engine(connection_string)

    def disconnect(self):
        """Close all pooled PostgreSQL connections and clear the engine."""
        if self.conn is not None:
            self.conn.dispose()
            self.conn = None


# if __name__ == "__main__":
#     config = read_config(Path("/Users/codebase/Documents/codebase/aurum/src/ingestion/configs/ohlcv_1d.yaml"))
#     config = config.get("output_datasource", "")
#     config["table"] = "sample"
#     pg = PostgresDataSource(config)
#     pg.connect()
#     pg.write_data(data=pd.DataFrame({
#         "date": pd.to_datetime(date.today(), unit="D"),
#         "open": np.random.randn(10000),
#     }))


