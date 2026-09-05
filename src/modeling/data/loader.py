"""Read the training panel out of Postgres, cached to Parquet.

The pull is the dominant cost of iterating on a model — ~2.9M rows by ~228 columns —
and the warehouse only changes when dbt runs. So the frame is cached against the
source table's ``max(date)`` and re-read from disk until the warehouse moves on.

The panel never exists in memory twice: the query streams from a server-side cursor
straight into a Parquet file, and only the finished file is read back.
"""

import logging
import os
import tempfile
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from src.modeling.config import ModelingConfig, SourceConfig
from src.utils.env import load_env

logger = logging.getLogger(__name__)

CHUNK_ROWS = 100_000

# How each Postgres type is pulled, keyed on the data_type information_schema reports:
# what to cast it to in SQL (None = take it as it comes) and the Arrow type it lands as.
#
# The casts exist because psycopg2 hands back Python objects that Arrow will not
# convert: `numeric` arrives as Decimal ("cannot be converted to float32") and `date`
# as datetime.date ("cannot be converted to int"). Casting in the query means Postgres
# sends a float or a timestamp and nothing needs converting on this side — that one
# move is what removes the whole pandas casting layer this loader used to carry.
#
# float32 for every number is deliberate, integers included. The panel's integer
# columns are ranks and flags (`*_decile`, `fold_id`, `day_of_week`) whose values are
# exact in float32, LightGBM consumes floats and NaNs natively, and a nullable integer
# comes back out of Parquet as float64 regardless.
PULL_TYPES: dict[str, tuple[str | None, pa.DataType]] = {
    "numeric": ("float8", pa.float32()),
    "double precision": ("float8", pa.float32()),
    "real": ("float8", pa.float32()),
    "smallint": ("float8", pa.float32()),
    "integer": ("float8", pa.float32()),
    "bigint": ("float8", pa.float32()),
    "date": ("timestamp", pa.timestamp("us")),
    "timestamp without time zone": (None, pa.timestamp("us")),
    "timestamp with time zone": (None, pa.timestamp("us")),
    "text": (None, pa.string()),
    "character varying": (None, pa.string()),
    "character": (None, pa.string()),
}


def _validate_identifier(identifier: str) -> str:
    """Guard an identifier that has to be interpolated rather than bound."""
    if not identifier or not identifier.replace("_", "").isalnum():
        raise ValueError(f"Invalid SQL identifier: {identifier!r}")
    return identifier


def _build_engine(source: SourceConfig) -> Engine:
    """Resolve the configured env var names and open a connection."""
    load_env()
    user = os.getenv(source.username)
    password = os.getenv(source.password)
    host = os.getenv(source.host)
    port = os.getenv(source.port)
    return create_engine(
        f"postgresql://{user}:{password}@{host}:{port}/{source.db_name}"
    )


def build_panel_query(
    declared_types: dict[str, str], qualified: str
) -> tuple[str, pa.Schema]:
    """Return the select statement and the Parquet schema it produces.

    Both come from the same declared types, so the SQL and the file schema cannot
    drift apart. The schema has to be explicit: left to inference, an all-null chunk
    of a column types as ``null`` and a chunk holding NaNs as ``double``, so the
    second chunk written would no longer match the first.
    """
    unknown = {
        column: data_type
        for column, data_type in declared_types.items()
        if data_type not in PULL_TYPES
    }
    if unknown:
        raise ValueError(f"Unsupported Postgres types: {unknown}")

    selected = []
    fields = []
    for column, data_type in declared_types.items():
        cast_to, arrow_type = PULL_TYPES[data_type]
        selected.append(f'"{column}"::{cast_to}' if cast_to else f'"{column}"')
        fields.append((column, arrow_type))
    return f"select {', '.join(selected)} from {qualified}", pa.schema(fields)


def read_declared_types(engine: Engine, schema: str, table: str) -> dict[str, str]:
    """Return ``{column: Postgres data_type}`` in table order."""
    query = text(
        "select column_name, data_type from information_schema.columns "
        "where table_schema = :schema and table_name = :table order by ordinal_position"
    )
    with engine.connect() as connection:
        rows = connection.execute(query, {"schema": schema, "table": table}).all()
    if not rows:
        raise ValueError(f"{schema}.{table} does not exist")
    return {name: data_type for name, data_type in rows}


def _download(engine: Engine, query: str, schema: pa.Schema, path: Path) -> None:
    """Stream the query into a Parquet file, one chunk at a time.

    ``stream_results=True`` is what makes this incremental. Without it psycopg2 uses
    a client-side cursor and buffers the whole result set before pandas sees the first
    row, so ``chunksize`` only slices something already resident.

    The write lands on a scratch file and is renamed once the footer is on disk. A run
    that dies mid-stream would otherwise leave a truncated Parquet where the cache is
    expected, and the cache check only tests that the path exists. The scratch name
    carries a uuid so two overlapping rebuilds cannot clobber each other's file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(f"{path.suffix}.{uuid4().hex[:8]}.partial")
    rows = 0
    try:
        with (
            engine.connect().execution_options(stream_results=True) as connection,
            pq.ParquetWriter(partial, schema, compression="snappy") as writer,
        ):
            for chunk in pd.read_sql(query, connection, chunksize=CHUNK_ROWS):
                writer.write_table(
                    pa.Table.from_pandas(chunk, schema=schema, preserve_index=False)
                )
                rows += len(chunk)
                logger.info("Streamed %s rows to %s", rows, path.name)
        if rows == 0:
            raise ValueError(f"{query} returned no rows")
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def load_training_frame(
    config: ModelingConfig, columns: list[str] | None = None
) -> pd.DataFrame:
    """Return the source table, from the Parquet cache when it is current.

    ``columns`` is passed through to the Parquet read, so a run that only needs the
    selected features never materialises the other ~190.
    """
    schema_name = _validate_identifier(config.source.db_schema)
    table = _validate_identifier(config.source.table)
    qualified = f'"{schema_name}"."{table}"'

    engine = _build_engine(config.source)
    try:
        with engine.connect() as connection:
            max_date = connection.execute(
                text(f"select max(date) from {qualified}")
            ).scalar()
        if max_date is None:
            raise ValueError(f"{schema_name}.{table} is empty")

        cache_path = config.cache.dir / f"{table}_{max_date}.parquet"
        if config.cache.enabled and cache_path.exists():
            logger.info("Reading cached frame from %s", cache_path)
            return pd.read_parquet(cache_path, columns=columns)

        logger.info("Reading %s from Postgres (max date %s)", qualified, max_date)
        declared_types = read_declared_types(engine, schema_name, table)
        query, schema = build_panel_query(declared_types, qualified)

        if config.cache.enabled:
            _download(engine, query, schema, cache_path)
            logger.info("Cached frame to %s", cache_path)
            return pd.read_parquet(cache_path, columns=columns)

        with tempfile.TemporaryDirectory() as tmp:
            spill = Path(tmp) / f"{table}.parquet"
            _download(engine, query, schema, spill)
            return pd.read_parquet(spill, columns=columns)
    finally:
        engine.dispose()
