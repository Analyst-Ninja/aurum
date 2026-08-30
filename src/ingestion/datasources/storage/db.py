from abc import abstractmethod
from typing import Dict, Any

import pandas as pd
import psycopg2

from src.ingestion.datasources.base_datasource import BaseDatasource
from src.ingestion.factory.registory import register_datasource


class Database(BaseDatasource):

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

    def read_data(self, config: Dict[str, Any]) -> pd.DataFrame:
        pass

    def write_data(self, config: Dict[str, Any], data):
        pass

    @abstractmethod
    def get_watermarks(self, config: Dict[str, Any]) -> pd.DataFrame:
        pass

@register_datasource("postgres")
class PostgresDataSource(Database):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.config = config
        self.conn = psycopg2.connect(**config)
        self.cur = self.conn.cursor()

    def get_watermarks(self, config: Dict[str, Any]) -> pd.DataFrame:
        pass


if __name__ == "__main__":
    pg = PostgresDataSource({})
    print(pg.logger)








