import logging
from datetime import date, datetime, timedelta
from itertools import batched
from typing import Any, Dict, Iterator
import yfinance as yf

import pandas as pd

from src.ingestion.factory.registory import register_datasource
from src.ingestion.datasources.base_datasource import BaseDatasource
from src.utils.env import get_sec_user_agent
from src.utils.symbols import get_snp500_symbols


@register_datasource("yahoo_ohlcv")
class OHLCVDataSource(BaseDatasource):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.config = config
        self.logger = logging.getLogger(type(self).__name__)
        self.user_agent = get_sec_user_agent()
        self.timeout = config.get("timeout", 100)

    def read_data(
        self,
        run_date: str,
        watermarks: Dict[str, date] | None = None,
    ) -> pd.DataFrame:
        """The whole pull as one frame. Prefer ``read_data_chunks`` — a first load with no
        watermark is 503 symbols from 2000 to today, and concatenating that here is what
        made the frame, not the write, the thing that would not fit in memory."""
        frames = list(self.read_data_chunks(run_date, watermarks))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def read_data_chunks(
        self,
        run_date: str,
        watermarks: Dict[str, date] | None = None,
    ) -> Iterator[pd.DataFrame]:
        """One frame per symbol batch, yielded as it is fetched.

        The batching is unchanged — the same ``batched(..., batch_size)`` loop, still
        serial, still self-throttled. The only difference is that a batch is handed to the
        caller instead of being appended to a list that is concatenated at the end.
        """
        symbols = get_snp500_symbols(self.user_agent, self.timeout)
        watermarks = watermarks or {}
        run_day = datetime.strptime(run_date, "%Y-%m-%d").date()
        groups: Dict[date | str, list[str]] = {}

        for symbol in symbols:
            watermark = watermarks.get(symbol)
            start = self.config.get("history_floor", (datetime.today() - timedelta(days=7)).strftime('%Y-%m-%d'))
            if watermark is not None:
                start = watermark + timedelta(days=1)
                if start >= run_day:
                    continue
            groups.setdefault(start, []).append(symbol)

        for start, grouped_symbols in groups.items():
            for chunk in batched(grouped_symbols, self.config.get("batch_size", 100)):
                raw = yf.Tickers(list(chunk)).history(
                    start=start,
                    end=run_day,
                    interval=self.config.get("interval", "1d"),
                    auto_adjust=False,
                )
                if raw is None or raw.empty:
                    self.logger.info("no rows for chunk starting %s", chunk[0])
                    continue
                yield self._normalize(raw)

    def write_data(self, run_date:str, data: pd.DataFrame) -> None:
        """Not Required for an API"""
        ...

    @staticmethod
    def _normalize(raw: pd.DataFrame) -> pd.DataFrame:
        """Multi-index yfinance history → long-form (date, symbol, fields) frame."""
        # De-fragment the frame yfinance built via repeated inserts before reshaping
        data = raw.copy()
        reshaped = data.stack(level=0)
        reshaped = (
            reshaped.rename_axis(index=["date", "ticker"])
            .reset_index(level=1)
            .reset_index()
        )
        long = reshaped.melt(
            id_vars=["date", "ticker"], var_name="symbol", value_name="value"
        ).rename(columns={"ticker": "field"})
        out = long.pivot_table(
            index=["date", "symbol"], columns="field", values="value"
        ).reset_index()
        return out.copy()
