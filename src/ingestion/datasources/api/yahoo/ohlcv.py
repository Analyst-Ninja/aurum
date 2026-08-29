from typing import Dict, Any

import pandas as pd

from src.ingestion.factory.registory import register_datasource
from src.ingestion.datasources.base_datasource import BaseDatasource


@register_datasource("yahoo_ohlcv")
class OHLCVDataSource(BaseDatasource):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

    def read_data(self, config: Dict[str, Any]) -> pd.DataFrame:
        ...

    def write_data(self, config: Dict[str, Any], data):
        pass

ohlcv = OHLCVDataSource({})
print(ohlcv.name)
print(ohlcv.logger)
print(ohlcv.logger.name)
