import logging
from abc import ABC, abstractmethod
from datetime import date
from typing import Any, Dict, Iterator

import pandas as pd


class BaseDatasource(ABC):

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.name = self.__class__.__name__
        self.logger = logging.getLogger(type(self).__name__)
        self.logger.setLevel(logging.INFO)

    @abstractmethod
    def read_data(self, run_date: str,  watermarks: Dict[str, date]) -> pd.DataFrame: ...

    @abstractmethod
    def write_data(self, run_date: str, data: pd.DataFrame): ...

    def read_data_chunks(
        self, run_date: str, watermarks: Dict[str, date] | None = None
    ) -> Iterator[pd.DataFrame]:
        """Yield the read in pieces, so the feed can write one before fetching the next.

        The default is one chunk — the whole frame — which is what ``read_data`` already
        returned, so a source that does not page keeps its current behaviour. Sources that
        fetch in batches override this and yield per batch; ``BaseFeed.run`` then holds one
        batch at a time instead of the entire pull.

        An empty frame yields nothing rather than an empty chunk, so callers can treat
        "no chunks" as "no data".
        """
        frame = self.read_data(run_date, watermarks=watermarks)
        if frame is not None and not frame.empty:
            yield frame