import logging
from abc import ABC, abstractmethod
from typing import Any, Dict

import pandas as pd


class BaseDatasource(ABC):

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.name = self.__class__.__name__
        self.logger = logging.getLogger(type(self).__name__)
        self.logger.setLevel(logging.INFO)

    @abstractmethod
    def read_data(self, config: Dict[str, Any]) -> pd.DataFrame: ...

    @abstractmethod
    def write_data(self, config: Dict[str, Any], data): ...