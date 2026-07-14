"""Contracts every datasource, sink, and state store implements."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import ClassVar, Protocol

import pandas as pd

from src.core.schemas import BaseRecord


@dataclass(frozen=True)
class FetchRequest:
    """What to fetch. Computed by the pipeline (from watermarks), honored by the source.

    ``start``/``end`` bound the window; ``end`` is exclusive. The window can
    span a day, a month, or several quarters — sources must not assume a cadence.
    """

    symbols: tuple[str, ...]
    start: date
    end: date
    interval: str = "1d"


class BatchDataSource(Protocol):
    """Pull-based source. Pure read: no DB, no Kafka, no sleep loops."""

    schema: ClassVar[type[BaseRecord]]

    def fetch(self, request: FetchRequest) -> Iterator[pd.DataFrame]:
        """Yield DataFrames conforming to ``schema``, one per API batch."""
        ...


class StateStore(Protocol):
    """Watermark storage keyed by (source, entity)."""

    def get_watermarks(self, source: str) -> dict[str, date]:
        """All watermarks for a source, keyed by entity (symbol)."""
        ...

    def set_watermark(self, source: str, entity: str, value: date) -> None: ...


class Sink(Protocol):
    """Writer for validated frames. Idempotency contract: safe to re-run."""

    def write(self, df: pd.DataFrame, *, table: str, natural_key: list[str]) -> int:
        """Write rows; returns rows written."""
        ...
