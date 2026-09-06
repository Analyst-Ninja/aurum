import pytest

from src.ingestion import cli


@pytest.fixture
def argv(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["src.ingestion.cli", "-c", "src/ingestion/configs/yahoo/ohlcv_1d.yaml", "-f", "False"],
    )


def _stub_run_feed(monkeypatch, metrics):
    monkeypatch.setattr(cli, "run_feed", lambda *args, **kwargs: metrics)


@pytest.mark.parametrize("status", ["SUCCESS", "SUCCESS_NO_DATA"])
def test_main_exits_zero_when_the_feed_did_not_fail(argv, monkeypatch, status):
    """SUCCESS_NO_DATA is a market holiday, not a failure — it must stay green."""
    _stub_run_feed(monkeypatch, {"execution_status": status, "row_count": 0})

    cli.main()  # no SystemExit


def test_main_exits_one_when_the_feed_failed(argv, monkeypatch):
    """BaseFeed.run() swallows the exception, so only the exit code carries the failure."""
    _stub_run_feed(
        monkeypatch,
        {"execution_status": "FAILED", "error_message": "connection refused"},
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1


def test_main_tolerates_a_metrics_dict_without_a_status(argv, monkeypatch):
    _stub_run_feed(monkeypatch, {})

    cli.main()  # no SystemExit
