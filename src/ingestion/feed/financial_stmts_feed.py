import pandas as pd

from src.ingestion.factory.registory import register_feed
from src.ingestion.feed.base_feed import BaseFeed


class _FinancialStatementsFeed(BaseFeed):
    """Shared `process()` for every EDGAR statement feed.

    All six feeds do exactly the same thing — uppercase the columns and drop the
    melt's leftover INDEX column. The only thing that differed was the log line, and
    the six copies had drifted: the cash-flow and balance-sheet feeds both logged
    "income statements". `statement_label` is now the single per-feed value.
    """

    statement_label = "financial statements"

    def process(self, data: pd.DataFrame) -> pd.DataFrame:
        """Uppercase the columns and drop the melt's INDEX column."""
        self.logger.info("Processing %s data", self.statement_label)

        data.columns = [col.upper() for col in data.columns]
        return data.drop(columns=["INDEX"], errors="ignore")


##################################################
# Income Statements Feed
##################################################
@register_feed("income_stmts_yearly")
class YearlyIncomeStatements(_FinancialStatementsFeed):
    statement_label = "yearly income statements"


@register_feed("income_stmts_quarterly")
class QuarterlyIncomeStatements(_FinancialStatementsFeed):
    statement_label = "quarterly income statements"


##################################################
# Cash Flow Statements Feed
##################################################
@register_feed("cashflow_stmts_yearly")
class YearlyCashFlowStatements(_FinancialStatementsFeed):
    statement_label = "yearly cash flow statements"


@register_feed("cashflow_stmts_quarterly")
class QuarterlyCashFlowStatements(_FinancialStatementsFeed):
    statement_label = "quarterly cash flow statements"


##################################################
# Balance Sheet Feed
##################################################
@register_feed("balance_sheet_stmts_yearly")
class YearlyBalanceSheetStatements(_FinancialStatementsFeed):
    statement_label = "yearly balance sheet statements"


@register_feed("balance_sheet_stmts_quarterly")
class QuarterlyBalanceSheetStatements(_FinancialStatementsFeed):
    statement_label = "quarterly balance sheet statements"
