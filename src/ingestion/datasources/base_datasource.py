import logging
from abc import ABC, abstractmethod
from datetime import date
from typing import Any, Dict

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