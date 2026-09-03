import pandas as pd

from src.ingestion.factory.registory import register_feed
from src.ingestion.feed.base_feed import BaseFeed

@register_feed("ohlcv_1d")
class Ohlcv1d(BaseFeed):

    def process(self, data: pd.DataFrame) -> pd.DataFrame:
        """Process OHLCV 1 day data"""

        self.logger.info("Processing OHLCV 1D data")

        cols = [col.upper() for col in data.columns]
        data.columns = cols

        return data

@register_feed("ohlcv_1min")
class Ohlcv1min(BaseFeed):

    def process(self, data: pd.DataFrame) -> pd.DataFrame:
        """Process OHLCV 1 min data"""

        self.logger.info("Processing OHLCV 1min data")

        cols = [col.upper() for col in data.columns]
        data.columns = cols

        return data