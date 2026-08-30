import logging
from datetime import datetime, timedelta
from itertools import batched
from logging import config
from pathlib import Path
from typing import Dict, Any, Generator
import yfinance as yf

import pandas as pd
from pandas import DataFrame

from src.ingestion.factory.registory import register_datasource
from src.ingestion.datasources.base_datasource import BaseDatasource
from src.utils.symbols import get_snp500_symbols
from src.utils.config_reader import read_config


@register_datasource("yahoo_ohlcv")
class OHLCVDataSource(BaseDatasource):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.config = config
        self.logger = logging.getLogger(type(self).__name__)
        self.user_agent = "a@gmai.com"
        self.timeout = 100
        print(config)

    def read_data(self):
        symbols = get_snp500_symbols(self.user_agent, self.timeout)[:200]
        frames = []
        for chunk in batched(symbols, self.config.get("batch_size", 10)):
            raw = yf.Tickers(list(chunk)).history(
                start=self.config.get("history_floor", "2020-01-01"),
                end=datetime.today() - timedelta(days=1),
                interval=self.config.get("interval", "1d"),
                auto_adjust=False,
            )
            if raw is None or raw.empty:
                self.logger.info("no rows for chunk starting %s", chunk[0])
                continue
            frames.append(self._normalize(raw))

        return pd.concat(frames)

    def write_data(self, data: pd.DataFrame) -> None:
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


config = read_config(Path("/Users/codebase/Documents/codebase/aurum/src/ingestion/configs/ohlcv_1d.yaml"))["input_datasource"]
ohlcv = OHLCVDataSource(config)
print(ohlcv.name)
print(ohlcv.logger)
print(ohlcv.logger.name)
df = ohlcv.read_data()
print(df.shape)
