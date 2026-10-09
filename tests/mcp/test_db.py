"""The MCP server's read path: the keyword guard, the row cap, and no commit.

Postgres is the real guard (READ ONLY transaction, read-only role); these tests pin the
parts that live in our code. No live database: the engine is replaced by a fake.
"""

import inspect

import pytest

from src.mcp import db


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select * from gold.mart_features;",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "EXPLAIN SELECT 1",
        "-- top names\nSELECT symbol FROM gold.mart_features",
        "/* note */ SELECT 1",
        "SELECT 'drop; table' AS note",
        'SELECT "update" FROM t',
        "SELECT updated_at, delete_flag FROM t",
    ],
)
def test_read_queries_pass_and_lose_the_trailing_semicolon(sql):
    assert not db.check_select(sql).endswith(";")


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "DROP TABLE gold.mart_features",
        "DELETE FROM gold.mart_features",
        "UPDATE gold.mart_features SET close = 0",
        "SELECT 1; DROP TABLE gold.mart_features",
        "/* SELECT */ DROP TABLE gold.mart_features",
        "WITH g AS (DELETE FROM gold.mart_features RETURNING *) SELECT * FROM g",
        "SELECT * FROM gold.mart_features FOR UPDATE",
        "SELECT * INTO scratch FROM gold.mart_features",
        "EXPLAIN ANALYZE DELETE FROM gold.mart_features",
        "SET ROLE postgres",
    ],
)
def test_anything_but_a_single_read_is_rejected(sql):
    with pytest.raises(ValueError):
        db.check_select(sql)


def test_is_read_query_is_the_boolean_form():
    assert db.is_read_query("SELECT 1")
    assert not db.is_read_query("DROP TABLE t")
    assert "DELETE" in db.read_query_problem("WITH g AS (DELETE FROM t) SELECT 1")


class FakeResult:
    def __init__(self, n):
        self.n = n

    def keys(self):
        return ["i"]

    def fetchmany(self, size):
        self.asked = size
        return [(i,) for i in range(min(size, self.n))]


class FakeConn:
    def __init__(self, result):
        self.result = result
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        return self.result

    def execution_options(self, **kw):
        return self

    def rollback(self):
        self.statements.append("ROLLBACK")


class FakeEngine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return self._conn


def _patch(monkeypatch, rows):
    conn = FakeConn(FakeResult(rows))
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(conn))
    monkeypatch.setattr(db, "max_rows_ceiling", lambda: 5)
    return conn


def test_the_transaction_is_read_only_and_rolled_back(monkeypatch):
    conn = _patch(monkeypatch, 1)

    db.run_select("SELECT 1")

    assert conn.statements[0] == "SET TRANSACTION READ ONLY"
    assert conn.statements[-1] == "ROLLBACK"


def test_one_extra_row_is_fetched_so_truncated_is_a_fact(monkeypatch):
    _patch(monkeypatch, 100)

    out = db.run_select("SELECT i FROM t", max_rows=3)

    assert out["row_count"] == 3
    assert out["truncated"] is True


def test_a_short_result_is_not_truncated(monkeypatch):
    _patch(monkeypatch, 2)

    assert db.run_select("SELECT i FROM t", max_rows=3)["truncated"] is False


def test_max_rows_is_clamped_to_the_ceiling(monkeypatch):
    _patch(monkeypatch, 100)

    assert db.run_select("SELECT i FROM t", max_rows=10_000)["row_count"] == 5


def test_the_module_never_commits():
    assert ".commit(" not in inspect.getsource(db)
