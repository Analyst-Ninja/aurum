import hashlib
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

    def _add_write_metadata(
        self, data: pd.DataFrame, run_date: str, execution_id: str
    ) -> pd.DataFrame:
        """Add run metadata and a deterministic key before persisting rows."""
        output_config = self.config.get("output_datasource", {})
        key_columns = output_config.get("cols_for_pk", [])
        hash_column = output_config.get("primary_key", "md5_hash")

        missing_columns = [column for column in key_columns if column not in data.columns]
        if missing_columns:
            raise ValueError(f"Primary-key columns missing from output: {missing_columns}")

        output = data.copy()
        output["RUN_DATE"] = run_date
        output["EXECUTION_ID"] = execution_id

        if not key_columns:
            raise ValueError("Output datasource must include cols_for_pk")

        key_values = output[key_columns].astype("string").fillna("<NULL>")
        output[hash_column] = key_values.apply(
            lambda row: hashlib.md5(
                "||".join(f"{column}={row[column]}" for column in key_columns).encode("utf-8")
            ).hexdigest(),
            axis=1,
        )
        return output

    @staticmethod
    def _generate_execution_id() -> str:
        """Generate a unique execution id"""
        return datetime.now().strftime("%Y%m%d_%H%M%S%f")

    @abstractmethod
    def process(self, data: pd.DataFrame) -> pd.DataFrame:
        pass

    def run(self, run_date: str, full_load: bool = True):
        """Main execution logic.

        Runs a full load by default; pass ``full_load=False`` to resume from the
        output datasource's watermarks.
        """

        execution_id = self._generate_execution_id()

        try:
            self.logger.info(f"Starting feed execution: {self.feed_name} | date: {run_date} | execution_id: {execution_id}")

            incremental = not full_load
            watermarks = {}
            if incremental:
                watermarks = self.output_ds.get_watermarks(
                    group_by=self.config.get("watermark_group_by", "symbol"),
                    date_column=self.config.get("watermark_date_column", "date"),
                )

            data = self.input_ds.read_data(run_date, watermarks=watermarks)
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
            processed_data = self._add_write_metadata(
                processed_data, run_date, execution_id
            )

            self.output_ds.write_data(run_date, processed_data)

            self.metrics["end_time"] = datetime.now()
            self.metrics["execution_status"] = "SUCCESS"
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

