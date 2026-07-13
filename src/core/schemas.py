"""Base record model. One subclass per landing table (docs/data-dictionary.md)."""

from typing import ClassVar

import pandas as pd
from pydantic import BaseModel, ConfigDict

from src.core.errors import SchemaMismatchError


class BaseRecord(BaseModel):
    """A single row of a landing table.

    Subclasses declare ``table`` and ``natural_key``; sinks and dedup read them
    from the schema so the key is defined exactly once. Batch paths validate
    whole DataFrames via ``validate_frame`` (column check — no per-row cost).
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    table: ClassVar[str]
    natural_key: ClassVar[list[str]]

    @classmethod
    def frame_columns(cls) -> list[str]:
        """DataFrame column names for this schema (field aliases win)."""
        return [field.alias or name for name, field in cls.model_fields.items()]

    @classmethod
    def validate_frame(cls, df: pd.DataFrame) -> pd.DataFrame:
        """Check required columns exist; return them in schema order."""
        columns = cls.frame_columns()
        missing = set(columns) - set(df.columns)
        if missing:
            raise SchemaMismatchError(
                f"{cls.__name__}: missing columns {sorted(missing)}"
            )
        return df[columns]
