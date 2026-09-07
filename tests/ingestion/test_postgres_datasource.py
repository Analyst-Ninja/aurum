"""`PostgresDataSource` — the watermark and freshness lookups, and identifier safety.

Schema, table and column names cannot be bound as query parameters, so they are
interpolated; `validate_identifier` is the only thing between a config and injection.

A missing landing table is normal on the first run — both lookups have to tell 42P01
apart from a real database error and return the "nothing loaded yet" answer.
"""

from datetime import date

import pandas as pd
import psycopg2
import pytest
from sqlalchemy.exc import ProgrammingError

from src.ingestion.datasources.storage import db


@pytest.fixture
def source():
    return db.PostgresDataSource(
        {
            "type": "postgres",
            "name": "sink",
            "table": "ohlcv_1d",
            "db_schema": "bronze",
            "db_name": "aurum",
            "host": "HOST",
            "port": "PORT",
            "username": "AURUM_USERNAME",
            "password": "AURUM_PASSWORD",
        }
    )


@pytest.fixture
def query(monkeypatch):
    """Capture the SQL and hand back a canned frame (or raise)."""
    seen = {}

    def _set(frame=None, error=None):
        def _read_sql(statement, connection):
            seen["sql"] = str(statement)
            if error is not None:
                raise error
            return frame

        monkeypatch.setattr(db.pd, "read_sql_query", _read_sql)
        return seen

    return _set


@pytest.fixture(autouse=True)
def no_engine(monkeypatch):
    monkeypatch.setattr(db, "create_engine", lambda url: ("engine", url))


def _missing_relation():
    """What psycopg2 raises for a table that does not exist yet, as SQLAlchemy wraps it."""
    original = psycopg2.errors.UndefinedTable('relation "bronze.ohlcv_1d" does not exist')
    return ProgrammingError("select 1", {}, original)


# --------------------------------------------------------------------------- identifiers


@pytest.mark.parametrize("identifier", ["ohlcv_1d", "SYMBOL", "bronze"])
def test_a_plain_identifier_is_returned_unchanged(identifier):
    assert db.validate_identifier(identifier) == identifier


@pytest.mark.parametrize(
    "identifier", ["", "a; drop table x", "public.t", 'a"b', "a-b", "a b"]
)
def test_anything_that_is_not_a_bare_identifier_is_rejected(identifier):
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        db.validate_identifier(identifier)


def test_missing_relation_walks_the_driver_exception_chain():
    assert db.is_missing_relation(_missing_relation())


def test_a_real_database_error_is_not_mistaken_for_a_missing_table():
    assert not db.is_missing_relation(ValueError("connection refused"))


def test_no_error_at_all_is_not_a_missing_table():
    assert not db.is_missing_relation(None)


# --------------------------------------------------------------------------- watermarks


def test_watermarks_are_the_latest_stored_date_per_group(source, query):
    query(
        frame=pd.DataFrame(
            {"group_key": ["AAPL", "MSFT"], "max_date": ["2026-09-04", "2026-09-05"]}
        )
    )

    assert source.get_watermarks("SYMBOL", "DATE") == {
        "AAPL": date(2026, 9, 4),
        "MSFT": date(2026, 9, 5),
    }


def test_the_watermark_query_groups_by_the_configured_columns(source, query):
    seen = query(frame=pd.DataFrame({"group_key": [], "max_date": []}))

    source.get_watermarks("SYMBOL", "DATE")

    assert 'MAX("DATE")' in seen["sql"]
    assert 'GROUP BY "SYMBOL"' in seen["sql"]
    assert '"bronze"."ohlcv_1d"' in seen["sql"]


def test_a_missing_landing_table_is_a_first_run_not_an_error(source, query):
    """`-f False` against a fresh RDS therefore pulls the whole history."""
    query(error=_missing_relation())

    assert source.get_watermarks() == {}


def test_a_real_watermark_error_is_raised(source, query):
    query(error=ProgrammingError("select 1", {}, ValueError("permission denied")))

    with pytest.raises(ProgrammingError):
        source.get_watermarks()


def test_an_empty_watermark_result_is_an_empty_dict(source, query):
    query(frame=pd.DataFrame())

    assert source.get_watermarks() == {}


def test_rows_with_a_null_symbol_or_date_are_skipped(source, query):
    query(
        frame=pd.DataFrame(
            {
                "group_key": ["AAPL", None, "MSFT"],
                "max_date": ["2026-09-04", "2026-09-04", None],
            }
        )
    )

    assert source.get_watermarks() == {"AAPL": date(2026, 9, 4)}


def test_watermarks_need_a_table(query):
    with pytest.raises(ValueError, match="must include a table"):
        db.PostgresDataSource({"name": "sink"}).get_watermarks()


def test_an_injected_group_by_never_reaches_the_query(source, query):
    seen = query(frame=pd.DataFrame())

    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        source.get_watermarks(group_by='symbol"; drop table x --')

    assert "sql" not in seen


# --------------------------------------------------------------------------- max value


def test_max_value_reads_the_single_scalar(source, query):
    query(frame=pd.DataFrame({"max_value": ["2026-09-01"]}))

    assert source.get_max_value("RUN_DATE") == "2026-09-01"


def test_the_max_value_query_names_the_column_and_the_table(source, query):
    seen = query(frame=pd.DataFrame({"max_value": ["2026-09-01"]}))

    source.get_max_value("RUN_DATE")

    assert 'MAX("RUN_DATE")' in seen["sql"]
    assert '"bronze"."ohlcv_1d"' in seen["sql"]


def test_a_missing_table_reads_as_nothing_loaded_yet(source, query):
    query(error=_missing_relation())

    assert source.get_max_value("RUN_DATE") is None


def test_a_real_max_value_error_is_raised(source, query):
    query(error=ProgrammingError("select 1", {}, ValueError("permission denied")))

    with pytest.raises(ProgrammingError):
        source.get_max_value("RUN_DATE")


def test_an_empty_table_reads_as_nothing_loaded_yet(source, query):
    """A table that exists but holds no rows means the same thing to a freshness gate."""
    query(frame=pd.DataFrame())

    assert source.get_max_value("RUN_DATE") is None


def test_a_null_max_reads_as_nothing_loaded_yet(source, query):
    query(frame=pd.DataFrame({"max_value": [None]}))

    assert source.get_max_value("RUN_DATE") is None


def test_max_value_needs_a_table():
    with pytest.raises(ValueError, match="must include a table"):
        db.PostgresDataSource({"name": "sink"}).get_max_value("RUN_DATE")


def test_an_injected_column_never_reaches_the_query(source, query):
    seen = query(frame=pd.DataFrame())

    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        source.get_max_value('RUN_DATE"; drop table x --')

    assert "sql" not in seen


# --------------------------------------------------------------------------- connection


def test_credentials_are_env_var_names_resolved_at_connect_time(source, monkeypatch):
    monkeypatch.setenv("AURUM_USERNAME", "aurum")
    monkeypatch.setenv("AURUM_PASSWORD", "secret")
    monkeypatch.setenv("HOST", "db.internal")
    monkeypatch.setenv("PORT", "5432")

    source.connect()

    assert source.conn == ("engine", "postgresql://aurum:secret@db.internal:5432/aurum")


def test_disconnect_disposes_the_pool_and_clears_the_engine(source):
    disposed = []
    source.conn = type("Engine", (), {"dispose": lambda self: disposed.append(True)})()

    source.disconnect()

    assert disposed == [True]
    assert source.conn is None


def test_disconnecting_twice_is_harmless(source):
    source.disconnect()

    assert source.conn is None


def test_read_data_is_unused_on_the_sink_side(source):
    assert source.read_data() is None
