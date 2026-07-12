"""
Yahoo Finance Batch Data Source
"""

from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
import time
from typing import Any
import pandas as pd
import yfinance as yf
import requests
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from tqdm import tqdm
from src.datasources.apis.api_ds import BaseAPIDataSource, POSTGRES_URL


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

    def _get_watermarks(self) -> dict[str, date]:
        """Latest stored bar date per symbol, read from the DB.

        Returns an empty mapping when the DB is unavailable or the table does
        not exist yet (first run), so every symbol is treated as brand-new.
        """
        if not POSTGRES_URL:
            return {}
        engine = create_engine(url=POSTGRES_URL, echo=False)
        try:
            df = pd.read_sql(
                "SELECT symbol, MAX(date) AS max_date FROM ohlcv_1d GROUP BY symbol",
                engine,
            )
        except (SQLAlchemyError, pd.errors.DatabaseError):
            return {}
        return {
            row.symbol: pd.to_datetime(row.max_date).date() for row in df.itertuples()
        }

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
            start=start_date,
            end=end_date,
            interval=interval,
            auto_adjust=False,
        )

        if data is None or data.empty:
            return pd.DataFrame(  # Return an empty frame when Yahoo returns no rows
                columns=["date", "symbol", "open", "high", "low", "close", "volume"]
            )

        # De-fragment the frame yfinance built via repeated inserts before reshaping
        data = data.copy()

        # Reshape the multi-index history into a long-form DataFrame without creating a fragmented intermediate frame
        reshaped = data.stack(level=0)
        reshaped = (
            reshaped.rename_axis(index=["date", "ticker"])
            .reset_index(level=1)
            .reset_index()
        )

        # Convert the reshaped frame to a wide OHLCV table
        long = reshaped.melt(
            id_vars=["date", "ticker"], var_name="symbol", value_name="value"
        ).rename(columns={"ticker": "field"})

        out = long.pivot_table(
            index=["date", "symbol"], columns="field", values="value"
        ).reset_index()

        return out.copy()

    def _group_by_start(self, symbols, start_date, end_date) -> dict[date, list[str]]:
        """Map each symbol to the start date it needs, then group by that date.

        A symbol absent from the DB is fetched in full from ``start_date``; a
        symbol with watermark ``d`` is fetched from ``d + 1 day``. Because
        ``end_date`` is exclusive, a symbol whose next start is ``>= end_date``
        has nothing new and is dropped entirely so it issues no API call.
        """
        watermarks = self._get_watermarks()
        groups: dict[Any, list] = {}
        for sym in symbols:
            wm = watermarks.get(sym)
            if wm is None:
                start = start_date
            else:
                start = wm + timedelta(days=1)
                # `end_date` is exclusive: an empty [start, end_date) window
                # means nothing new, so skip the request entirely
                if start >= end_date:
                    continue  # already up to date -> no request
            groups.setdefault(start, []).append(sym)
        return groups

    def read_data(
        self,
        symbols,
        start_date="2000-01-01",
        end_date=None,
        interval="1d",
        batch_size=100,
        sleep_seconds=10,
    ):
        """
        Incrementally retrieve OHLCV data, fetching only bars newer than the DB.

        :param symbols: List of stock symbols to retrieve data for.
        :param start_date: Full-history floor for symbols not yet in the DB
            (YYYY-MM-DD); defaults to 2000-01-01. Symbols already in the DB
            ignore this and resume from their watermark.
        :param end_date: End date for the data retrieval (YYYY-MM-DD).
        :param interval: Data interval (e.g., "1d" for daily, "1h" for hourly).
        :param batch_size: Number of symbols to process in each batch.
        :param sleep_seconds: Throttle between batches to respect API rate limits.
        :return: DataFrame containing the OHLCV data.
        """
        # Constant for the whole run; groups every row ingested by this invocation
        run_date = datetime.now(timezone.utc).date()
        # Yahoo's `end` is exclusive; default to today when the caller omits it
        end_date_dt = (
            datetime.strptime(end_date, "%Y-%m-%d").date()
            if end_date is not None
            else run_date
        )

        groups = self._group_by_start(symbols, start_date, end_date_dt)

        batch_frames = []
        for start, grp_symbols in groups.items():
            for i in tqdm(
                range(0, len(grp_symbols), batch_size),
                total=max(1, (len(grp_symbols) + batch_size - 1) // batch_size),
                desc=f"Fetching OHLCV batches (from {start})",
            ):
                batch_symbols = grp_symbols[i : i + batch_size]
                batch_data = self._read_batch_data(
                    batch_symbols, start, end_date_dt, interval
                )
                if batch_data.empty:
                    continue  # nothing new for this batch; skip the DB write
                # Stamp ingestion metadata just before the DB write
                batch_data = batch_data[
                    [
                        "date",
                        "symbol",
                        "Adj Close",
                        "Close",
                        "Dividends",
                        "High",
                        "Low",
                        "Open",
                        "Stock Splits",
                        "Volume",
                    ]
                ].copy()
                batch_data = batch_data.assign(
                    run_date=run_date,
                    inserted_at=datetime.now(timezone.utc),
                )
                batch_frames.append(batch_data)
                self.write_data(batch_data)  # Write the batch data to the database
                time.sleep(sleep_seconds)  # Throttle to avoid hitting rate limits

        if batch_frames:
            self.data = pd.concat(batch_frames, ignore_index=True)
        else:
            self.data = pd.DataFrame(
                columns=["date", "symbol", "open", "high", "low", "close", "volume"]
            )

        return self.data


if __name__ == "__main__":
    # Example usage
    ohlcv_data_source = OHLCVDataSource()
    sym = ohlcv_data_source.get_symbols()  # Get the first 200 symbols for demonstration
    START_DATE = "2000-01-01"
    END_DATE = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    df = ohlcv_data_source.read_data(
        sym, START_DATE, END_DATE, interval="1d", batch_size=100
    )
