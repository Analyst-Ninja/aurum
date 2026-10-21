import subprocess

import pandas as pd
import pytest

from src.paper.backtesting import runner
from src.paper.backtesting.inputs import EngineResult, target_schedule
from tests.paper.test_backtesting import config, synthetic

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(scope="module")
def inputs():
    scores, bars = synthetic()
    cfg = config()
    stamps = pd.DatetimeIndex(sorted(bars["timestamp"].unique()))
    return cfg, scores, bars, target_schedule(cfg, scores, stamps)


def test_vectorbt_empty_schedule_reports_error(inputs):
    pytest.importorskip("vectorbt")
    from src.paper.backtesting import vectorbt_engine

    cfg, _, bars, _ = inputs
    result = vectorbt_engine.run(cfg, {}, bars)

    assert result.error == "empty schedule"
    assert result.equity.empty


def test_backtrader_skips_orders_below_min_usd(inputs):
    pytest.importorskip("backtrader")
    from src.paper.backtesting import backtrader_engine

    cfg, _, bars, schedule = inputs
    result = backtrader_engine.run(config(min_order_usd=1e12), schedule, bars)

    assert result.error is None
    assert result.orders.empty
    assert result.equity.iloc[-1] == pytest.approx(cfg.capital)


def test_backtrader_subsamples_wide_intraday_universe(inputs, monkeypatch):
    pytest.importorskip("backtrader")
    from src.paper.backtesting import backtrader_engine

    _, _, bars, schedule = inputs
    monkeypatch.setattr(backtrader_engine, "MAX_30MIN_SYMBOLS", 10)
    result = backtrader_engine.run(config(bar_interval="30Min"), schedule, bars)

    assert result.note.startswith("subsampled to 11 symbols")
    assert set(result.orders["symbol"]) <= set(bars["symbol"])
    assert result.orders["symbol"].nunique() <= 11


def fake_child(returncode=0, files=None, error=None):
    """Stand-in for subprocess.run that writes what the lumibot child would leave in cwd."""

    def run(command, *, cwd, **kwargs):
        if error:
            raise error
        for name, text in (files or {}).items():
            (cwd / name).write_text(text)
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr="boom\nlast line")

    return run


def test_lumibot_run_reads_child_output(inputs, monkeypatch):
    from src.paper.backtesting import lumibot_engine

    cfg, _, bars, schedule = inputs
    files = {
        "equity.csv": ",equity\n2026-01-06 00:00:00+00:00,100000\n2026-01-07 00:00:00+00:00,101000\n",
        "orders.csv": "ts,symbol,side,qty,price\n2026-01-06,S01,buy,10,50\n",
    }
    monkeypatch.setattr(lumibot_engine.subprocess, "run", fake_child(files=files))
    result = lumibot_engine.run(cfg, schedule, bars)

    assert result.error is None
    assert result.equity.iloc[-1] == 101000
    assert str(result.equity.index.tz) == "UTC"
    assert list(result.orders["symbol"]) == ["S01"]


def test_lumibot_run_tolerates_missing_orders_file(inputs, monkeypatch):
    from src.paper.backtesting import lumibot_engine

    cfg, _, bars, schedule = inputs
    files = {"equity.csv": ",equity\n2026-01-06 00:00:00+00:00,100000\n"}
    monkeypatch.setattr(lumibot_engine.subprocess, "run", fake_child(files=files))
    result = lumibot_engine.run(cfg, schedule, bars)

    assert result.error is None
    assert result.orders.empty


def test_lumibot_run_reports_child_failure_tail(inputs, monkeypatch):
    from src.paper.backtesting import lumibot_engine

    cfg, _, bars, schedule = inputs
    monkeypatch.setattr(lumibot_engine.subprocess, "run", fake_child(returncode=2))
    result = lumibot_engine.run(cfg, schedule, bars)

    assert result.error.startswith("child exited 2")
    assert "last line" in result.error


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("uv", 1), FileNotFoundError("uv")])
def test_lumibot_run_reports_launch_errors(inputs, monkeypatch, error):
    from src.paper.backtesting import lumibot_engine

    cfg, _, bars, schedule = inputs
    monkeypatch.setattr(lumibot_engine.subprocess, "run", fake_child(error=error))
    result = lumibot_engine.run(cfg, schedule, bars)

    assert result.error.startswith(type(error).__name__)


def test_lumibot_daily_collapses_intraday_bars_to_sessions():
    from src.paper.backtesting.lumibot_engine import _daily

    stamps = pd.to_datetime(["2026-01-06 14:30", "2026-01-06 15:00", "2026-01-07 14:30"], utc=True)
    bars = pd.DataFrame(
        {
            "symbol": "AAA",
            "timestamp": stamps,
            "open": [10.0, 11.0, 20.0],
            "close": [10.5, 12.0, 19.0],
        }
    )
    daily = _daily(bars, pd.DataFrame(index=stamps[:1])).sort_values("session")

    assert list(daily["open"]) == [10.0, 20.0]
    assert list(daily["close"]) == [12.0, 19.0]
    assert list(daily["high"]) == [12.0, 20.0]
    assert list(daily["low"]) == [10.0, 19.0]


@pytest.fixture
def stub_engine(monkeypatch):
    """Replace the vectorbt module the runner imports with one whose run() is supplied."""
    import sys
    import types

    def install(run):
        module = types.ModuleType("src.paper.backtesting.vectorbt_engine")
        module.run = run
        monkeypatch.setitem(sys.modules, "src.paper.backtesting.vectorbt_engine", module)
        import src.paper.backtesting as package

        monkeypatch.setattr(package, "vectorbt_engine", module, raising=False)

    return install


def test_run_engine_sim_matches_direct_simulation(inputs):
    cfg, scores, bars, schedule = inputs
    result = runner._run_engine("sim", cfg, scores, bars, schedule)

    assert result.error is None
    assert result.name == "sim"
    assert len(result.orders) > 0


def test_run_engine_rejects_unknown_name(inputs):
    cfg, scores, bars, schedule = inputs
    result = runner._run_engine("zipline", cfg, scores, bars, schedule)

    assert result.error.startswith("ValueError")
    assert "zipline" in result.error


def test_run_engine_turns_missing_library_into_install_hint(inputs, stub_engine):
    def missing(*_):
        raise ImportError("No module named 'vectorbt'")

    stub_engine(missing)
    cfg, scores, bars, schedule = inputs
    result = runner._run_engine("vectorbt", cfg, scores, bars, schedule)

    assert "not installed" in result.error
    assert "--group backtesting" in result.error


def test_run_engine_isolates_a_crashing_engine(inputs, stub_engine):
    def crash(*_):
        raise RuntimeError("bad bar")

    stub_engine(crash)
    cfg, scores, bars, schedule = inputs
    result = runner._run_engine("vectorbt", cfg, scores, bars, schedule)

    assert result.error == "RuntimeError: bad bar"


def test_run_engine_returns_the_engine_result(inputs, stub_engine):
    stub_engine(lambda *_: EngineResult("vectorbt", note="stub"))
    cfg, scores, bars, schedule = inputs
    result = runner._run_engine("vectorbt", cfg, scores, bars, schedule)

    assert result.note == "stub"


def write_inputs(tmp_path):
    scores, bars = synthetic()
    scores.to_parquet(tmp_path / "scores.parquet")
    bars.to_parquet(tmp_path / "bars.parquet")
    return str(tmp_path / "scores.parquet"), str(tmp_path / "bars.parquet")


def test_run_all_saves_report_for_sim(tmp_path, capsys):
    scores_file, bars_file = write_inputs(tmp_path)
    cfg = config(out_dir=str(tmp_path / "out"))
    run_dir = runner.run_all(cfg, "2026-01-05", "2026-03-06", ["sim"], ["W"], scores_file, bars_file)

    assert (run_dir / "report.html").exists()
    assert "Saved" in capsys.readouterr().out


def test_run_all_prints_skipped_engines(tmp_path, capsys):
    scores_file, bars_file = write_inputs(tmp_path)
    cfg = config(out_dir=str(tmp_path / "out"))
    runner.run_all(cfg, "2026-01-05", "2026-03-06", ["sim", "zipline"], ["W"], scores_file, bars_file)

    assert "SKIPPED zipline" in capsys.readouterr().out


def test_run_all_rejects_empty_score_window(tmp_path):
    scores_file, bars_file = write_inputs(tmp_path)
    cfg = config()
    with pytest.raises(ValueError, match="No scores between"):
        runner.run_all(cfg, "2030-01-01", "2030-02-01", ["sim"], ["W"], scores_file, bars_file)


def test_run_all_rejects_bars_that_miss_the_score_dates(tmp_path):
    scores_file, bars_file = write_inputs(tmp_path)
    bars = pd.read_parquet(bars_file)
    bars.assign(timestamp=bars["timestamp"] - pd.Timedelta(days=730)).to_parquet(bars_file)
    cfg = config()
    with pytest.raises(ValueError, match="No executions scheduled"):
        runner.run_all(cfg, "2026-01-05", "2026-03-06", ["sim"], ["W"], scores_file, bars_file)
