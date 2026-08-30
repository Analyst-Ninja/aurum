import os
from abc import abstractmethod, ABC
from datetime import date
from pathlib import Path
from typing import Dict, Any

import numpy as np
from dotenv import load_dotenv
import pandas as pd
import psycopg2
from sqlalchemy import create_engine

from src.ingestion.datasources.base_datasource import BaseDatasource
from src.ingestion.factory.registory import register_datasource
from src.utils.config_reader import read_config

load_dotenv("/Users/codebase/Documents/codebase/aurum/.env")

class Database(BaseDatasource):

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.conn = None

    def read_data(self) -> pd.DataFrame:
        pass

    def write_data(self, data: pd.DataFrame) -> None:
        # Fast bulk load method using PostgreSQL's COPY syntax
        data.to_sql(
            name=self.config.get("table", ""),
            con=self.conn,
            if_exists='append',
            index=False,
            method='multi',
            chunksize=10000
        )

    @abstractmethod
    def get_watermarks(self, config: Dict[str, Any]) -> pd.DataFrame:
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

    def get_watermarks(self, config: Dict[str, Any]) -> pd.DataFrame:
        pass

    def connect(self):
        user = os.getenv(self.config.get("username", ""))
        password = os.getenv(self.config.get("password", ""))
        host = os.getenv(self.config.get("host", ""))
        port = os.getenv(self.config.get("port", ""))
        db_name = self.config.get("db_name", "")

        connection_string = f"postgresql://{user}:{password}@{host}:{port}/{db_name}"

        self.logger.info(f"Creating engine for {connection_string}")
        self.conn = create_engine(connection_string)

    def disconnect(self):
        pass


if __name__ == "__main__":
    config = read_config(Path("/Users/codebase/Documents/codebase/aurum/src/ingestion/configs/ohlcv_1d.yaml"))
    config = config.get("output_datasource", "")
    config["table"] = "sample"
    pg = PostgresDataSource(config)
    pg.connect()
    pg.write_data(data=pd.DataFrame({
        "date": pd.to_datetime(date.today(), unit="D"),
        "open": np.random.randn(10000),
    }))









