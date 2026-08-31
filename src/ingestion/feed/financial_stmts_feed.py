import pandas as pd

from src.ingestion.factory.registory import register_feed
from src.ingestion.feed.base_feed import BaseFeed

##################################################
# Income Statements Feed
##################################################
@register_feed("income_stmts_yearly")
class YearlyIncomeStatements(BaseFeed):

    def process(self, data: pd.DataFrame) -> pd.DataFrame:
        """Process OHLCV 1 day data"""

        self.logger.info("Processing yearly income statements data")

        cols = [col.upper() for col in data.columns]
        data.columns = cols

        data = data.drop(columns=['INDEX'], errors='ignore')

        return data

@register_feed("income_stmts_quarterly")
class QuarterlyIncomeStatements(BaseFeed):

    def process(self, data: pd.DataFrame) -> pd.DataFrame:
        """Process OHLCV 1 day data"""

        self.logger.info("Processing quarterly income statements data")

        cols = [col.upper() for col in data.columns]
        data.columns = cols

        data = data.drop(columns=['INDEX'], errors='ignore')

        return data

##################################################
# Cash Flow Statements Feed
##################################################

@register_feed("cashflow_stmts_yearly")
class YearlyCashFlowStatements(BaseFeed):

    def process(self, data: pd.DataFrame) -> pd.DataFrame:
        """Process OHLCV 1 day data"""

        self.logger.info("Processing yearly income statements data")

        cols = [col.upper() for col in data.columns]
        data.columns = cols

        data = data.drop(columns=['INDEX'], errors='ignore')

        return data

@register_feed("cashflow_stmts_quarterly")
class QuarterlyCashFlowStatements(BaseFeed):

    def process(self, data: pd.DataFrame) -> pd.DataFrame:
        """Process OHLCV 1 day data"""

        self.logger.info("Processing quarterly income statements data")

        cols = [col.upper() for col in data.columns]
        data.columns = cols

        data = data.drop(columns=['INDEX'], errors='ignore')

        return data

##################################################
# Balance Sheet Feed
##################################################

@register_feed("balance_sheet_stmts_yearly")
class YearlyBalanceSheetStatements(BaseFeed):

    def process(self, data: pd.DataFrame) -> pd.DataFrame:
        """Process OHLCV 1 day data"""

        self.logger.info("Processing yearly income statements data")

        cols = [col.upper() for col in data.columns]
        data.columns = cols

        data = data.drop(columns=['INDEX'], errors='ignore')

        return data

@register_feed("balance_sheet_stmts_quarterly")
class QuarterlyBalanceSheetStatements(BaseFeed):

    def process(self, data: pd.DataFrame) -> pd.DataFrame:
        """Process OHLCV 1 day data"""

        self.logger.info("Processing quarterly income statements data")

        cols = [col.upper() for col in data.columns]
        data.columns = cols

        data = data.drop(columns=['INDEX'], errors='ignore')

        return data