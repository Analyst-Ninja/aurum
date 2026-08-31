import logging
from datetime import date, datetime, timedelta
from itertools import batched
from typing import Dict, Any
import yfinance as yf

import pandas as pd

from src.ingestion.factory.registory import register_datasource
from src.ingestion.datasources.base_datasource import BaseDatasource
from src.utils.symbols import get_snp500_symbols


@register_datasource("yahoo_ohlcv")
class OHLCVDataSource(BaseDatasource):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.config = config
        self.logger = logging.getLogger(type(self).__name__)
        self.user_agent = "a@gmail.com"
        self.timeout = 100

    def read_data(
        self,
        run_date: str,
        watermarks: Dict[str, date] | None = None,
    ) -> pd.DataFrame:
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

        frames = []
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
                frames.append(self._normalize(raw))

        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

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


# if __name__ == "__main__":
#     config = read_config(Path("/Users/codebase/Documents/codebase/aurum/src/ingestion/configs/ohlcv_1d.yaml"))["input_datasource"]
#     ohlcv = OHLCVDataSource(config)
#     print(ohlcv.name)
#     print(ohlcv.logger)
#     print(ohlcv.logger.name)
#     df = ohlcv.read_data(datetime.today().strftime("%Y-%m-%d"))
#     print(df.shape)
