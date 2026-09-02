import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Dict, Any

import pandas as pd
from edgar import Company, set_identity

from src.ingestion.factory.registory import register_datasource
from src.ingestion.datasources.base_datasource import BaseDatasource
from src.utils.config_reader import read_config
from src.utils.env import get_sec_user_agent
from src.utils.symbols import get_snp500_symbols

@register_datasource("edgar_income_stmts")
class IncomeStmtsDatasource(BaseDatasource):
    """Data source for income statements"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.config = config
        self.logger = logging.getLogger(type(self).__name__)
        self.user_agent = get_sec_user_agent()
        self.timeout = config.get("timeout", 10)

    def _melt_income_statement(self, df: pd.DataFrame, ticker: str) -> pd.DataFrame:
        """Melt a wide income statement (concept rows, FY period columns) into long format."""
        df = df.reset_index()  # bring 'concept' out of the index into a column

        # Identify FY period columns vs identifier columns
        fy_cols = [c for c in df.columns if str(c).startswith("FY")]
        id_vars = [c for c in df.columns if c not in fy_cols]

        df_melted = pd.melt(df, id_vars=id_vars, value_vars=fy_cols,
                            var_name='FY', value_name='value')

        # Uppercase all column names
        df_melted.columns = [x.upper() for x in df_melted.columns]

        # Tag which ticker this row belongs to
        df_melted['SYMBOL'] = ticker

        return df_melted

    def _get_income_statement(self, ticker: str, periods: int = 20):
        """Fetch and melt the income statement for a single ticker. Returns (ticker, df_or_exception)."""
        try:
            company = Company(ticker)
            income = company.income_statement(periods=periods)
            df = income.to_dataframe()
            df_melted = self._melt_income_statement(df, ticker)
            return ticker, df_melted
        except Exception as e:
            return ticker, e

    def _get_all_income_statements(self, tickers: list[str], max_workers: int = 10) -> dict:
        """Fetch and melt income statements for all tickers concurrently using a thread pool."""
        results = {}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_ticker = {
                executor.submit(self._get_income_statement, ticker): ticker
                for ticker in tickers
            }

            for future in as_completed(future_to_ticker):
                ticker, result = future.result()
                results[ticker] = result
                if isinstance(result, Exception):
                    print(f"{ticker}: FAILED - {result}")

        return results

    def _combine_results(self, results: dict) -> pd.DataFrame:
        """Stack all successfully-melted DataFrames into one long DataFrame."""
        frames = [df for df in results.values() if isinstance(df, pd.DataFrame)]
        return pd.concat(frames, ignore_index=True)

    def read_data(self, run_date: str,  watermarks: Dict[str, date]) -> pd.DataFrame:
        """Read data for income statement and melt it into long format."""

        symbols = get_snp500_symbols(self.user_agent, self.timeout)

        set_identity(self.user_agent)

        data = self._get_all_income_statements(tickers=symbols, max_workers=self.config.get("max_workers", 10))

        return self._combine_results(results=data)

    def write_data(self, run_date: str, data: pd.DataFrame) -> None:
        ...

if __name__ == "__main__":
    config_path = Path("/Users/codebase/Documents/codebase/aurum/src/ingestion/configs/edgar/income_statements_yearly.yaml")
    config = read_config(config_path).get("input_datasource")
    obj = IncomeStmtsDatasource(config)
    res = obj.read_data(run_date="2026-01-01",watermarks={})
    print(res.head().columns)
