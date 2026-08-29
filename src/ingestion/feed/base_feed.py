import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, Any

import pandas as pd

from src.ingestion.factory import factory


class BaseFeed(ABC):

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.feed_name = config.get("feed_name", "Unknown")
        self.input_ds = factory.create_datasource(self.config.get("input_datasource", ""))
        self.output_ds = factory.create_datasource(self.config.get("output_datasource", ""))
        self.logger = logging.getLogger(f"Feed_{self.feed_name}")
        self.logger.setLevel(logging.DEBUG)

        self.start_time = datetime.now()
        self.metrics = {}

    def _emit_metrics(self):
        duration = datetime.now() - self.start_time

        self.logger.info(f"Metrics: Duration: {duration}")

    @staticmethod
    def _generate_execution_id() -> str:
        """Generate a unique execution id"""
        return datetime.now().strftime("%Y%m%d_%H%M%S%f")

    @abstractmethod
    def process(self, data: pd.DataFrame) -> pd.DataFrame:
        pass

    def run(self, run_date: str, full_load: bool= True):
        """Main execution logic"""

        execution_id = self._generate_execution_id()

        try:
            self.logger.info(f"Starting feed execution: {self.feed_name} | date: {run_date} | execution_id: {execution_id}")

            incremental = not full_load
            data = self.input_ds.read(run_date, incremental=incremental)
            self.metrics["incremental"] = incremental
            self.metrics["run_date"] = run_date
            self.metrics["execution_id"] = execution_id
            self.metrics["row_count"] = len(data)

            if data.empty:
                self.metrics["row_count"] = 0
                self.logger.warning(f"Feed {self.feed_name} has no data")
                self.metrics["end_time"] = datetime.now()
                self.metrics["execution_status"] = "SUCCESS_NO_DATA"
                return self.metrics

            processed_data = self.process(data)

            self.output_ds.write(processed_data)

            self.metrics["end_time"] = datetime.now()
            self.metrics["execution_status"] = "SUCCESS_NO_DATA"
            self.logger.info(f"Feed {self.feed_name} execution complete")

        except Exception as e:
            self.logger.error(f"Error starting feed execution: {e}")
            self.metrics["end_time"] = datetime.now()
            self.metrics["error_message"] = str(e)
            self.metrics["execution_id"] = execution_id
            self.metrics["execution_status"] = "FAILED"

        finally:
            self.logger.info(f"Feed {self.feed_name} execution complete")

        return self.metrics



