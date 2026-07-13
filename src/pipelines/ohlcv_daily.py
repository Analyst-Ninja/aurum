"""Incremental OHLCV pipeline: fetch only bars newer than the landing table.

Composition layer (spec §9): watermark math, throttling, and writes live here —
the datasource stays a pure API client.
"""

import logging
import time
from datetime import date, datetime, timedelta, timezone

from src.core.interfaces import BatchDataSource, FetchRequest, Sink, StateStore
from src.datasources.apis.yahoo.config import YahooConfig

logger = logging.getLogger(__name__)


class OhlcvDailyPipeline:
    """Watermark-driven incremental ingest of daily OHLCV bars."""

    SOURCE = "yahoo_ohlcv"

    def __init__(
        self,
        source: BatchDataSource,
        state: StateStore,
        sink: Sink,
        config: YahooConfig,
    ):
        self._source = source
        self._state = state
        self._sink = sink
        self._config = config

    def run(self, symbols: list[str], end: date | None = None) -> int:
        """Fetch the gap per symbol and write each frame as it arrives."""
        run_date = datetime.now(timezone.utc).date()
        end = end or run_date  # exclusive, matching Yahoo's `end`
        written = 0
        for start, group in self._group_by_start(symbols, end).items():
            request = FetchRequest(
                symbols=tuple(group),
                start=start,
                end=end,
                interval=self._config.interval,
            )
            for frame in self._source.fetch(request):
                frame = frame.assign(
                    run_date=run_date,
                    inserted_at=datetime.now(timezone.utc),
                )
                written += self._sink.write(
                    frame,
                    table=self._source.schema.table,
                    natural_key=self._source.schema.natural_key,
                )
                self._advance_watermarks(frame)
                time.sleep(self._config.sleep_seconds)  # throttle between writes
        logger.info("run complete: %d rows written", written)
        return written

    def _group_by_start(self, symbols: list[str], end: date) -> dict[date, list[str]]:
        """Symbol → next start (watermark + 1 day), grouped; up-to-date symbols dropped."""
        watermarks = self._state.get_watermarks(self.SOURCE)
        groups: dict[date, list[str]] = {}
        for sym in symbols:
            wm = watermarks.get(sym)
            start = (
                self._config.history_floor if wm is None else wm + timedelta(days=1)
            )
            # end is exclusive: an empty [start, end) window means nothing new
            if start < end:
                groups.setdefault(start, []).append(sym)
        return groups

    def _advance_watermarks(self, frame) -> None:
        """Record the max written date per symbol (after a successful write only)."""
        for sym, max_date in frame.groupby("symbol")["date"].max().items():
            self._state.set_watermark(self.SOURCE, sym, max_date)
