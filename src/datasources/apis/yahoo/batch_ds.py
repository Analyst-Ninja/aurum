"""
Yahoo Finance Batch Data Source
"""

from abc import ABC, abstractmethod
import time
from typing import Any
import pandas as pd
import yfinance as yf
import requests
from src.datasources.apis.api_ds import BaseAPIDataSource


class YahooFinanceBatchDataSource(BaseAPIDataSource, ABC):
    """
    A class to handle batch data retrieval from Yahoo Finance.
    """

    def __init__(self):
        pass  # Initialize any required attributes here

    @abstractmethod
    def read_data(self, symbols, start_date, end_date):
        """
        Retrieve batch data for the given symbols between the specified start and end dates.

        :param symbols: List of stock symbols to retrieve data for.
        :param start_date: Start date for the data retrieval (YYYY-MM-DD).
        :param end_date: End date for the data retrieval (YYYY-MM-DD).
        :return: DataFrame containing the batch data.
        """
        # Implement the logic to fetch batch data from Yahoo Finance

    def get_symbols(self) -> list[Any]:
        """
        Retrieve a list of symbols from SEC GOV.

        :return: List of available symbols.
        """
        # Implement the logic to fetch available symbols from SEC GOV
        ticker_url = "https://www.sec.gov/files/company_tickers.json"
        headers = {"User-Agent": "email@address.com"}

        response = requests.get(ticker_url, headers=headers, timeout=100)
        try:
            response.raise_for_status()
        except requests.HTTPError as err:
            raise requests.HTTPError(
                f"Failed to retrieve symbols from SEC GOV. Status code: {response.status_code}"
            ) from err

        res = response.json()
        symbols = [item["ticker"] for item in res.values()]
        return symbols


class OHLCVDataSource(YahooFinanceBatchDataSource):
    """
    A class to handle OHLCV (Open, High, Low, Close, Volume) data retrieval from Yahoo Finance.
    """

    def __init__(self):
        """initialize the OHLCVDataSource class."""
        super().__init__()  # Call the parent class constructor

    def _read_batch_data(
        self, symbols, start_date, end_date, interval="1d"
    ) -> pd.DataFrame:
        """
        Retrieve OHLCV data for the given symbols between the specified start and end dates.

        :param symbols: List of stock symbols to retrieve data for.
        :param start_date: Start date for the data retrieval (YYYY-MM-DD).
        :param end_date: End date for the data retrieval (YYYY-MM-DD).
        :param interval: Data interval (e.g., "1d" for daily, "1h" for hourly).
        :return: DataFrame containing the OHLCV data.
        """
        # Implement the logic to fetch OHLCV data from Yahoo Finance
        tickers = yf.Tickers(symbols)
        data = tickers.history(
            start=start_date, end=end_date, interval=interval, auto_adjust=False
        )

        if data is None or data.empty:
            return pd.DataFrame(  # Return an empty frame when Yahoo returns no rows
                columns=["date", "symbol", "open", "high", "low", "close", "volume"]
            )

        # Reshape the multi-index history into a long-form DataFrame
        data = (
            data.stack(0).rename_axis(["date", "ticker"]).reset_index(1).reset_index()
        )

        # Reshape the DataFrame to have a long format with 'date', 'symbol', and 'value' columns
        long = data.melt(
            id_vars=["date", "ticker"], var_name="symbol", value_name="value"
        ).rename(columns={"ticker": "field"})

        # Pivot the long DataFrame to have 'date' and 'symbol' as index, and 'field' as columns
        out = long.pivot_table(
            index=["date", "symbol"], columns="field", values="value"
        ).reset_index()

        return out

    def read_data(self, symbols, start_date, end_date, interval="1d", batch_size=100):
        """
        Public method to retrieve OHLCV data for the given symbols between the specified start and end dates.

        :param symbols: List of stock symbols to retrieve data for.
        :param start_date: Start date for the data retrieval (YYYY-MM-DD).
        :param end_date: End date for the data retrieval (YYYY-MM-DD).
        :param interval: Data interval (e.g., "1d" for daily, "1h" for hourly).
        :param batch_size: Number of symbols to process in each batch.
        :return: DataFrame containing the OHLCV data.
        """
        self.data = pd.DataFrame()

        for i in range(0, len(symbols), batch_size):
            batch_symbols = symbols[i : i + batch_size]
            batch_data = self._read_batch_data(
                batch_symbols, start_date, end_date, interval
            )
            self.data = pd.concat([self.data, batch_data])
            time.sleep(1)  # Sleep for 1 second to avoid hitting rate limits

        return self.data


if __name__ == "__main__":
    # Example usage
    ohlcv_data_source = OHLCVDataSource()
    sym = ohlcv_data_source.get_symbols()[
        :200
    ]  # Get the first 200 symbols for demonstration
    START_DATE = "2023-01-01"
    END_DATE = "2023-12-31"
    df = ohlcv_data_source.read_data(sym, START_DATE, END_DATE, interval="1d")
    ohlcv_data_source.write_data(df)  # Write the data to the database
    # print(df.head())
