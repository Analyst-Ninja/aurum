"""Yahoo Finance OHLCV batch datasource — pure fetch (spec §8).

No DB access, no sleeps: watermarks and pacing live in the pipeline.
"""

import logging
from collections.abc import Iterator
from itertools import batched
from typing import ClassVar

import pandas as pd
import yfinance as yf

from src.core.interfaces import FetchRequest
from src.datasources.apis.yahoo.config import OHLCVDailyConfig, OHLCVMinuteConfig
from src.datasources.apis.yahoo.schemas import OhlcvBar

logger = logging.getLogger(__name__)


class YahooOhlcvDataSource:
    """Batch OHLCV bars from Yahoo Finance."""

    schema: ClassVar[type[OhlcvBar]] = OhlcvBar

    def __init__(self, config: OHLCVDailyConfig | OHLCVMinuteConfig):
        self._config = config

    def fetch(self, request: FetchRequest) -> Iterator[pd.DataFrame]:
        """Yield one validated frame per symbol chunk; skip empty responses."""
        for chunk in batched(request.symbols, self._config.batch_size):
            raw = yf.Tickers(list(chunk)).history(
                start=request.start,
                end=request.end,
                interval=request.interval,
                auto_adjust=False,
            )
            if raw is None or raw.empty:
                logger.info("no rows for chunk starting %s", chunk[0])
                continue
            yield self.schema.validate_frame(self._normalize(raw))

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
