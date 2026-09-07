"""The pull path `test_loader.py` stops short of: engine construction, the type probe
and the two uncached branches of `load_training_frame`.

A real SQLite engine stands in for Postgres wherever the SQL is portable — the point is
that the streaming write and the rename actually happen, not that they are called.
"""

from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from src.modeling.config import ModelingConfig
from src.modeling.data import loader


@pytest.fixture
def source_config():
    return ModelingConfig(
        source={
            "db_schema": "gold",
            "table": "mart_training_set",
            "db_name": "aurum",
            "host": "HOST",
            "port": "PORT",
            "username": "AURUM_USERNAME",
            "password": "AURUM_PASSWORD",
        },
        target="fwd_ret_5d_excess",
    ).source


def test_credentials_are_env_var_names_resolved_at_connect_time(
    source_config, monkeypatch
):
    seen = {}
    monkeypatch.setattr(loader, "load_env", lambda: None)
    monkeypatch.setattr(loader, "create_engine", lambda url: seen.setdefault("url", url))
    monkeypatch.setenv("AURUM_USERNAME", "aurum")
    monkeypatch.setenv("AURUM_PASSWORD", "secret")
    monkeypatch.setenv("HOST", "db.internal")
    monkeypatch.setenv("PORT", "5432")

    loader._build_engine(source_config)

    assert seen["url"] == "postgresql://aurum:secret@db.internal:5432/aurum"


def _sqlite(*schemas):
    """In-memory SQLite with `gold` / `information_schema` attached as real schemas.

    `StaticPool` keeps the one connection the ATTACHes live on, so the schemas are
    still there when the loader opens its own.
    """
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as connection:
        for schema in schemas:
            connection.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {schema}")
    return engine


def _engine_with_types(rows):
    engine = _sqlite("information_schema")
    pd.DataFrame(rows).to_sql("columns", engine, schema="information_schema", index=False)
    return engine


def test_declared_types_come_back_in_table_order():
    engine = _engine_with_types(
        [
            {"table_schema": "gold", "table_name": "t", "column_name": "symbol",
             "data_type": "text", "ordinal_position": 1},
            {"table_schema": "gold", "table_name": "t", "column_name": "ret_21d",
             "data_type": "numeric", "ordinal_position": 2},
        ]
    )

    types = loader.read_declared_types(engine, "gold", "t")

    assert list(types) == ["symbol", "ret_21d"]
    assert types["ret_21d"] == "numeric"


def test_a_table_with_no_columns_does_not_exist():
    engine = _engine_with_types(
        [{"table_schema": "gold", "table_name": "other", "column_name": "a",
          "data_type": "text", "ordinal_position": 1}]
    )

    with pytest.raises(ValueError, match="gold.t does not exist"):
        loader.read_declared_types(engine, "gold", "t")


def _panel_db(rows):
    engine = _sqlite("gold", "information_schema")
    pd.DataFrame(rows).to_sql("mart_training_set", engine, schema="gold", index=False)
    pd.DataFrame(
        [
            {"table_schema": "gold", "table_name": "mart_training_set",
             "column_name": "symbol", "data_type": "text", "ordinal_position": 1},
            {"table_schema": "gold", "table_name": "mart_training_set",
             "column_name": "ret_21d", "data_type": "numeric", "ordinal_position": 2},
        ]
    ).to_sql("columns", engine, schema="information_schema", index=False)
    return engine


@pytest.fixture
def panel_engine():
    """A fresh two-row panel per call.

    `load_training_frame` disposes the engine in a `finally`, and disposing a
    StaticPool closes the one connection the ATTACHed schemas live on — so a test that
    calls it twice needs a new database the second time.
    """

    def _build(source):
        return _panel_db(
            {"symbol": ["AAPL", "MSFT"], "date": ["2026-09-01", "2026-09-02"],
             "ret_21d": [0.1, 0.2]}
        )

    return _build


@pytest.fixture
def config(tmp_path, panel_engine, monkeypatch):
    monkeypatch.setattr(loader, "_build_engine", panel_engine)
    # SQLite has no `::` cast syntax, so only the cast half of the query is stubbed;
    # `build_panel_query` itself is covered by its own tests.
    monkeypatch.setattr(
        loader, "build_panel_query",
        lambda types, qualified: (
            f'select "symbol", "ret_21d" from {qualified}',
            loader.pa.schema([("symbol", loader.pa.string()),
                              ("ret_21d", loader.pa.float32())]),
        ),
    )
    return ModelingConfig(
        source={
            "db_schema": "gold",
            "table": "mart_training_set",
            "db_name": "aurum",
            "host": "HOST",
            "port": "PORT",
            "username": "AURUM_USERNAME",
            "password": "AURUM_PASSWORD",
        },
        target="fwd_ret_5d_excess",
        cache={"dir": tmp_path / "cache", "enabled": True},
    )


def test_the_pull_is_cached_against_the_tables_max_date(config, tmp_path):
    frame = loader.load_training_frame(config)

    assert frame["symbol"].tolist() == ["AAPL", "MSFT"]
    assert (tmp_path / "cache" / "mart_training_set_2026-09-02.parquet").exists()


def test_a_second_call_reads_the_cache_rather_than_pulling_again(config, monkeypatch):
    loader.load_training_frame(config)

    def _never(*args, **kwargs):  # pragma: no cover — the point is that it is not called
        raise AssertionError("the cache should have short-circuited the pull")

    monkeypatch.setattr(loader, "_download", _never)

    assert len(loader.load_training_frame(config)) == 2


def test_only_the_requested_columns_come_back(config):
    frame = loader.load_training_frame(config, columns=["symbol"])

    assert list(frame.columns) == ["symbol"]


def test_with_the_cache_off_the_pull_spills_to_a_temporary_file(config, tmp_path):
    config.cache.enabled = False

    frame = loader.load_training_frame(config)

    assert len(frame) == 2
    assert not (tmp_path / "cache").exists()


def test_an_empty_source_table_is_refused_before_anything_is_written(
    config, tmp_path, monkeypatch
):
    """`max(date)` of an empty table is NULL, and a cache keyed on NULL is meaningless."""
    empty = _panel_db({"symbol": [], "date": [], "ret_21d": []})
    monkeypatch.setattr(loader, "_build_engine", lambda source: empty)

    with pytest.raises(ValueError, match="is empty"):
        loader.load_training_frame(config)


def test_an_injected_table_name_never_reaches_the_query(config):
    config.source.table = 'mart"; drop table x --'

    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        loader.load_training_frame(config)


def test_an_injected_schema_name_never_reaches_the_query(config):
    config.source.db_schema = "gold; drop table x"

    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        loader.load_training_frame(config)


def test_the_cache_path_is_a_path_not_a_string(config):
    assert isinstance(config.cache.dir, Path)
