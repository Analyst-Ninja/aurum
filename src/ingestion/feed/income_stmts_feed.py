import pandas as pd

from src.ingestion.factory.registory import register_feed
from src.ingestion.feed.base_feed import BaseFeed


@register_feed("income_stmts_yearly")
class Ohlcv1d(BaseFeed):

    def process(self, data: pd.DataFrame) -> pd.DataFrame:
        """Process OHLCV 1 day data"""

        self.logger.info("Processing yearly income statements data")

        cols = [col.upper() for col in data.columns]
        data.columns = cols

        data = data.drop(columns=['INDEX'], errors='ignore')

        return data

@register_feed("income_stmts_quarterly")
class Ohlcv1d(BaseFeed):

    def process(self, data: pd.DataFrame) -> pd.DataFrame:
        """Process OHLCV 1 day data"""

        self.logger.info("Processing quarterly income statements data")

        cols = [col.upper() for col in data.columns]
        data.columns = cols

        data = data.drop(columns=['INDEX'], errors='ignore')

        return data