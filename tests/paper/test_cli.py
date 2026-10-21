import sys
import types

import pandas as pd
import pytest

from src.paper import cli
from src.paper.config import PaperConfig


@pytest.fixture
def run_cli(monkeypatch):
    """Invoke cli.main() with argv, a stock config, and no log reconfiguration."""
    config = PaperConfig()
    monkeypatch.setattr(cli, "load_paper_config", lambda path: config)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["cli", *argv])
        cli.main()
        return config

    return run


def stub_module(monkeypatch, name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)


def test_periods_default_to_config_and_normalise_case():
    config = PaperConfig()

    assert cli._periods(types.SimpleNamespace(periods=None), config) == config.report_periods
    assert cli._periods(types.SimpleNamespace(periods="w, q"), config) == ["W", "Q"]


def test_rebalance_passes_flags_to_live(run_cli, monkeypatch):
    calls = []
    stub_module(monkeypatch, "src.paper.live", rebalance=lambda *a: calls.append(a), snapshot=None, watch=None)
    config = run_cli("rebalance", "--dry-run", "--force", "--asof", "2026-01-05")

    assert calls == [(config, "2026-01-05", True, True)]


def test_snapshot_prints_and_watch_loops(run_cli, monkeypatch, capsys):
    watched = []
    stub_module(monkeypatch, "src.paper.live", rebalance=None, snapshot=lambda c: "snap", watch=watched.append)
    config = run_cli("snapshot")

    assert "snap" in capsys.readouterr().out
    assert watched == []

    run_cli("snapshot", "--watch")
    assert watched == [config]


def test_report_without_snapshots_exits_nonzero(run_cli, monkeypatch):
    class Empty:
        def __init__(self, url): ...

        def snapshots(self):
            return pd.DataFrame()

    stub_module(monkeypatch, "src.paper.ledger", Ledger=Empty)
    with pytest.raises(SystemExit) as exit_info:
        run_cli("report")

    assert exit_info.value.code == 1


def test_report_renders_snapshots(run_cli, monkeypatch, capsys):
    index = pd.to_datetime(["2026-01-05", "2026-01-06"], utc=True)

    class Filled:
        def __init__(self, url): ...

        def snapshots(self):
            return pd.DataFrame({"equity": [100.0, 101.0], "benchmark": [10.0, 10.1]}, index=index)

    stub_module(monkeypatch, "src.paper.ledger", Ledger=Filled)
    monkeypatch.setattr(cli.report, "render", lambda *a: "RENDERED")
    run_cli("report", "-p", "W")

    assert "RENDERED" in capsys.readouterr().out


def test_backtest_applies_overrides_and_runs_sim_only(run_cli, monkeypatch):
    calls = []
    stub_module(monkeypatch, "src.paper.backtesting.runner", run_all=lambda *a: calls.append(a))
    config = run_cli(
        "backtest", "--start", "2026-01-01", "--end", "2026-02-01",
        "--bar-interval", "1Day", "--cost-bps", "7", "--model-version", "v1",
    )

    assert (config.bar_interval, config.cost_bps, config.model_version) == ("1Day", 7.0, "v1")
    assert calls[0][3] == ["sim"]


def test_crosscheck_splits_engine_list(run_cli, monkeypatch):
    calls = []
    stub_module(monkeypatch, "src.paper.backtesting.runner", run_all=lambda *a: calls.append(a))
    run_cli("crosscheck", "--start", "2026-01-01", "--end", "2026-02-01", "--engines", "sim, vectorbt")

    assert calls[0][3] == ["sim", "vectorbt"]


def test_backtest_failure_exits_one(run_cli, monkeypatch):
    def boom(*a):
        raise ValueError("no scores")

    stub_module(monkeypatch, "src.paper.backtesting.runner", run_all=boom)
    with pytest.raises(SystemExit) as exit_info:
        run_cli("backtest", "--start", "2026-01-01", "--end", "2026-02-01")

    assert exit_info.value.code == 1
