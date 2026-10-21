"""Audit trail for live paper trading: decisions, orders, snapshots.

SQLAlchemy URL from config; SQLite by default so a laptop needs no server, Postgres works
unchanged. Alpaca stays the source of truth for fills; this is the record of what we asked.
"""

from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, inspect, text


class Ledger:
    def __init__(self, url: str):
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url)

    def _has(self, table: str) -> bool:
        return inspect(self.engine).has_table(table)

    def append(self, table: str, frame: pd.DataFrame) -> None:
        if len(frame):
            frame.to_sql(table, self.engine, if_exists="append", index=False)

    def read(self, table: str) -> pd.DataFrame:
        return pd.read_sql(f"SELECT * FROM {table}", self.engine) if self._has(table) else pd.DataFrame()

    def decided(self, asof: str) -> bool:
        if not self._has("decisions"):
            return False
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT 1 FROM decisions WHERE asof = :a LIMIT 1"), {"a": asof}).first()
        return row is not None

    def snapshots(self) -> pd.DataFrame:
        frame = self.read("snapshots")
        if frame.empty:
            return frame
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
        return frame.sort_values("ts").set_index("ts")
