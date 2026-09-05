import pandas as pd
import pyarrow as pa
import pytest

from src.modeling.data.loader import (
    _validate_identifier,
    build_panel_query,
    load_training_frame,
)

# What information_schema reports for a slice of gold.mart_training_set.
DECLARED_TYPES = {
    "symbol": "text",
    "date": "date",
    "revenue": "numeric",
    "ret_21d": "double precision",
    "roe_decile": "integer",
}


def test_panel_query_casts_every_number_to_float8():
    # The cast is what stops psycopg2 handing back Decimals, which Arrow cannot
    # convert to float32 — the reason this loader used to need a pandas cast layer.
    query, schema = build_panel_query(DECLARED_TYPES, '"gold"."mart_training_set"')

    assert '"revenue"::float8' in query
    assert '"ret_21d"::float8' in query
    assert '"roe_decile"::float8' in query
    assert query.endswith('from "gold"."mart_training_set"')
    assert schema.field("revenue").type == pa.float32()
    assert schema.field("roe_decile").type == pa.float32()


def test_panel_query_casts_dates_to_timestamp_but_leaves_text_alone():
    # psycopg2 returns a `date` column as datetime.date objects, which Arrow will not
    # convert to a timestamp ("cannot be converted to int"). Casting in SQL means
    # Postgres sends a timestamp and nothing needs converting here.
    query, schema = build_panel_query(DECLARED_TYPES, '"gold"."mart_training_set"')

    assert '"date"::timestamp' in query
    assert '"symbol",' in query
    assert '"symbol"::' not in query
    assert schema.field("symbol").type == pa.string()
    assert schema.field("date").type == pa.timestamp("us")


def test_chunk_conversion_accepts_the_objects_psycopg2_actually_returns():
    # Guards both conversions Arrow refuses on raw psycopg2 output. A chunk of a
    # cast query holds floats and datetimes, never Decimal or datetime.date.
    from datetime import datetime

    _, schema = build_panel_query(DECLARED_TYPES, '"gold"."t"')
    chunk = pd.DataFrame(
        {
            "symbol": pd.Series(["AAPL", None], dtype="object"),
            "date": pd.Series([datetime(2026, 9, 1), None], dtype="object"),
            "revenue": pd.Series([1.5, None], dtype="object"),
            "ret_21d": pd.Series([None, 0.02], dtype="object"),
            "roe_decile": pd.Series([7.0, None], dtype="object"),
        }
    )

    table = pa.Table.from_pandas(chunk, schema=schema, preserve_index=False)

    assert table.schema.equals(schema)
    assert table.column("date")[0].as_py() == datetime(2026, 9, 1)


def test_panel_query_keeps_column_order():
    _, schema = build_panel_query(DECLARED_TYPES, '"gold"."t"')

    assert schema.names == list(DECLARED_TYPES)


def test_panel_query_rejects_an_unsupported_type():
    with pytest.raises(ValueError, match="blob"):
        build_panel_query({"blob": "bytea"}, '"gold"."t"')


def test_schema_is_identical_for_an_all_null_and_a_populated_chunk():
    # The ParquetWriter failure this guards: inference types an all-null chunk as
    # `null` and a populated one as `double`, so the second chunk written is rejected
    # with "Table schema does not match schema used to create file". Pinning the
    # schema to the declared types removes the inference entirely.
    _, schema = build_panel_query(DECLARED_TYPES, '"gold"."t"')
    columns = list(DECLARED_TYPES)

    empty = pd.DataFrame({column: pd.Series([None], dtype="object") for column in columns})
    populated = pd.DataFrame(
        {
            "symbol": ["AAPL"],
            "date": [pd.Timestamp("2026-09-01")],
            "revenue": [1.5],
            "ret_21d": [0.01],
            "roe_decile": [7.0],
        }
    )

    for frame in (empty, populated):
        table = pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
        assert table.schema.equals(schema)


def test_chunk_conversion_keeps_nulls_and_values():
    _, schema = build_panel_query(DECLARED_TYPES, '"gold"."t"')
    chunk = pd.DataFrame(
        {
            "symbol": ["AAPL", None],
            "date": [pd.Timestamp("2026-09-01"), None],
            "revenue": pd.Series([1.5, None], dtype="object"),
            "ret_21d": pd.Series([None, 0.02], dtype="object"),
            "roe_decile": pd.Series([7.0, None], dtype="object"),
        }
    )

    frame = pa.Table.from_pandas(chunk, schema=schema, preserve_index=False).to_pandas()

    assert frame["revenue"].tolist() == [1.5, None] or frame["revenue"].isna().iloc[1]
    assert frame["revenue"].iloc[0] == 1.5
    # A rank survives the float32 round trip exactly, and its null stays a null.
    assert frame["roe_decile"].iloc[0] == 7.0
    assert frame["roe_decile"].isna().iloc[1]
    assert frame["symbol"].iloc[0] == "AAPL"


def _sqlite_engine(rows):
    """A tiny real engine, so _download is exercised rather than mocked."""
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    frame = pd.DataFrame({"a": rows})
    frame.to_sql("t", engine, index=False)
    return engine


def test_download_renames_into_place_only_once_complete(tmp_path):
    from src.modeling.data.loader import _download

    target = tmp_path / "panel.parquet"
    _download(
        _sqlite_engine([1.0, 2.0, 3.0]),
        "select a from t",
        pa.schema([("a", pa.float32())]),
        target,
    )

    assert pd.read_parquet(target)["a"].tolist() == [1.0, 2.0, 3.0]
    assert list(tmp_path.glob("*.partial")) == []


def test_download_leaves_no_cache_and_no_scratch_when_it_fails(tmp_path):
    """A dying run must not leave a truncated Parquet where the cache is expected.

    The cache check is only `path.exists()`, so a partial file would be accepted as
    a complete panel — that happened, and trained on 50,000 of 2.9M rows.
    """
    from src.modeling.data.loader import _download

    target = tmp_path / "panel.parquet"
    with pytest.raises(Exception):
        _download(
            _sqlite_engine(["not a number"]),
            "select a from t",
            pa.schema([("a", pa.float32())]),
            target,
        )

    assert not target.exists()
    assert list(tmp_path.glob("*.partial")) == []


def test_download_rejects_an_empty_result(tmp_path):
    from src.modeling.data.loader import _download

    target = tmp_path / "panel.parquet"
    with pytest.raises(ValueError, match="returned no rows"):
        _download(
            _sqlite_engine([1.0]),
            "select a from t where a < 0",
            pa.schema([("a", pa.float32())]),
            target,
        )

    assert not target.exists()


def test_validate_identifier_rejects_injection():
    assert _validate_identifier("mart_training_set") == "mart_training_set"

    for bad in ('drop"', "a; select 1", ""):
        with pytest.raises(ValueError):
            _validate_identifier(bad)


def test_load_training_frame_reads_the_cache_without_touching_postgres(tmp_path, monkeypatch):
    """A current cache must short-circuit the pull, including the column subset."""
    from src.modeling.config import CacheConfig, ModelingConfig, SourceConfig
    from src.modeling.data import loader

    cached = pd.DataFrame(
        {"symbol": ["AAPL"], "ret_21d": [0.01], "roe_decile": [7.0]}
    )
    cached.to_parquet(tmp_path / "mart_training_set_2026-09-02.parquet", index=False)

    class _FakeConnection:
        def execute(self, *_args, **_kwargs):
            class _Result:
                @staticmethod
                def scalar():
                    return "2026-09-02"

            return _Result()

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class _FakeEngine:
        def connect(self):
            return _FakeConnection()

        def dispose(self):
            pass

    monkeypatch.setattr(loader, "_build_engine", lambda _source: _FakeEngine())
    monkeypatch.setattr(
        loader,
        "read_declared_types",
        lambda *_args: pytest.fail("cache hit must not query Postgres"),
    )

    config = ModelingConfig(
        source=SourceConfig(
            db_schema="gold",
            table="mart_training_set",
            db_name="aurum",
            host="HOST",
            port="PORT",
            username="AURUM_USERNAME",
            password="AURUM_PASSWORD",
        ),
        target="fwd_ret_5d_excess",
        cache=CacheConfig(dir=tmp_path, enabled=True),
    )

    frame = load_training_frame(config, columns=["symbol", "roe_decile"])

    assert list(frame.columns) == ["symbol", "roe_decile"]
    assert frame["roe_decile"].iloc[0] == 7.0
