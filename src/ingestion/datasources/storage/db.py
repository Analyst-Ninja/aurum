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

class Database(BaseDatasource):

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.conn = None

    def read_data(self) -> pd.DataFrame:
        pass

    def write_data(self, run_date: str, data: pd.DataFrame) -> None:
        # Fast bulk load method using PostgreSQL's COPY syntax
        self.connect()
        self.logger.info(f"Writing data to {run_date}")
        data.to_sql(
            name=self.config.get("table", ""),
            con=self.conn,
            if_exists='append',
            index=False
        )

        self.logger.info(f"Wrote data to {run_date}")
        self.disconnect()

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
            if not identifier or not identifier.replace("_", "").isalnum():
                raise ValueError(f"Invalid SQL identifier: {identifier!r}")

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
            original_error = error
            error_codes = set()
            error_messages = []
            while original_error is not None:
                error_codes.add(getattr(original_error, "pgcode", None))
                error_messages.append(str(original_error).lower())
                original_error = getattr(original_error, "orig", None)

            missing_relation = (
                "42P01" in error_codes
                or "undefinedtable" in " ".join(error_messages)
                or "does not exist" in " ".join(error_messages)
            )
            if missing_relation:
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


