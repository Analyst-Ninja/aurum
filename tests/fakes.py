"""In-memory test doubles for core interfaces."""

from datetime import date

import pandas as pd


class InMemoryStateStore:
    """Dict-backed StateStore for tests."""

    def __init__(self, initial: dict[str, date] | None = None):
        self._wm: dict[str, date] = dict(initial or {})

    def get_watermarks(self, source: str) -> dict[str, date]:
        return dict(self._wm)

    def set_watermark(self, source: str, entity: str, value: date) -> None:
        self._wm[entity] = value


class ListSink:
    """Sink that captures written frames."""

    def __init__(self):
        self.written: list[pd.DataFrame] = []

    def write(self, df: pd.DataFrame, *, table: str, natural_key: list[str]) -> int:
        self.written.append(df.copy())
        return len(df)
