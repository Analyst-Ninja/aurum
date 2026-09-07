import pytest
from sqlalchemy.exc import ProgrammingError

from src.ingestion import truncate


class _FakeConnection:
    def __init__(self, recorder, error=None):
        self._recorder = recorder
        self._error = error

    def execute(self, statement):
        if self._error is not None:
            raise self._error
        self._recorder.append(str(statement))

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeEngine:
    def __init__(self, recorder, error=None):
        self._recorder = recorder
        self._error = error

    def begin(self):
        return _FakeConnection(self._recorder, self._error)

    def dispose(self):
        self._recorder.append("dispose")


def _patch(monkeypatch, config, recorder, error=None):
    monkeypatch.setattr(truncate, "read_config", lambda _path: config)

    def _connect(self):
        self.conn = _FakeEngine(recorder, error)

    monkeypatch.setattr(truncate.PostgresDataSource, "connect", _connect)


def _config(**overrides):
    output = {
        "type": "postgres",
        "db_schema": "public",
        "table": "income_stmts_quarterly",
        "host": "HOST",
        "port": "PORT",
        "username": "AURUM_USERNAME",
        "password": "AURUM_PASSWORD",
        "db_name": "aurum",
    }
    output.update(overrides)
    return {"output_datasource": output}


def _missing_relation_error():
    error = ProgrammingError("TRUNCATE", {}, Exception('relation "x" does not exist'))
    error.orig.pgcode = "42P01"
    return error


def test_truncates_the_configured_table(monkeypatch):
    recorder = []
    _patch(monkeypatch, _config(), recorder)

    assert truncate.truncate_landing_table("cfg.yaml") == "public.income_stmts_quarterly"
    assert 'TRUNCATE TABLE "public"."income_stmts_quarterly"' in recorder
    assert "dispose" in recorder  # the engine is always released


@pytest.mark.parametrize("identifier", ["foo; DROP TABLE bar", "foo bar", "", "foo-bar"])
def test_rejects_identifiers_that_cannot_be_interpolated_safely(monkeypatch, identifier):
    """Schema and table cannot be bound as parameters, so they are validated instead."""
    recorder = []
    _patch(monkeypatch, _config(table=identifier), recorder)

    with pytest.raises(ValueError):
        truncate.truncate_landing_table("cfg.yaml")

    assert recorder == []


def test_a_missing_table_is_a_no_op(monkeypatch):
    """First run: the write path creates the table, so there is nothing to truncate."""
    recorder = []
    _patch(monkeypatch, _config(), recorder, error=_missing_relation_error())

    assert truncate.truncate_landing_table("cfg.yaml") == "public.income_stmts_quarterly"


def test_rejects_a_non_postgres_output(monkeypatch):
    recorder = []
    _patch(monkeypatch, _config(type="s3"), recorder)

    with pytest.raises(ValueError):
        truncate.truncate_landing_table("cfg.yaml")


def test_main_exits_one_when_the_truncate_fails(monkeypatch):
    """The ECS `&&` chain and runTask.sync both key on the exit code."""
    monkeypatch.setattr("sys.argv", ["src.ingestion.truncate", "-c", "cfg.yaml"])
    monkeypatch.setattr(
        truncate,
        "truncate_landing_table",
        lambda _path, _run_date: (_ for _ in ()).throw(ValueError("boom")),
    )

    with pytest.raises(SystemExit) as excinfo:
        truncate.main()

    assert excinfo.value.code == 1


def test_main_exits_zero_on_success(monkeypatch):
    monkeypatch.setattr("sys.argv", ["src.ingestion.truncate", "-c", "cfg.yaml"])
    monkeypatch.setattr(truncate, "truncate_landing_table", lambda _path, _run_date: "public.t")

    truncate.main()  # no SystemExit
