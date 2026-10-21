import numpy as np
import pandas as pd
import pytest

from src.paper.backtest import simulate
from src.paper.backtesting.inputs import target_schedule
from src.paper.config import PaperConfig

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

N_SYMBOLS, N_DAYS = 40, 45


def synthetic(seed=0):
    """Daily bars stamped at ET midnight (as Alpaca stamps 1Day bars) plus persistent scores."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2026-01-05", periods=N_DAYS, tz="America/New_York")
    names = [f"S{i:02d}" for i in range(N_SYMBOLS)] + ["SPY"]
    rows = []
    for n in names:
        close = 50 * np.exp(np.cumsum(rng.normal(0, 0.01, N_DAYS)))
        open_ = close * (1 + rng.normal(0, 0.002, N_DAYS))
        rows += [(n, d.tz_convert("UTC"), float(o), float(c)) for d, o, c in zip(days, open_, close)]
    bars = pd.DataFrame(rows, columns=["symbol", "timestamp", "open", "close"])
    quality = rng.normal(size=N_SYMBOLS)
    scores = pd.DataFrame(
        [
            (d.tz_localize(None), f"S{i:02d}", quality[i] + rng.normal(0, 0.3))
            for d in days[:-1]
            for i in range(N_SYMBOLS)
        ],
        columns=["date", "symbol", "score"],
    )
    return scores, bars


def config(**kw):
    base = dict(capital=100_000, cost_bps=0, horizon=5, min_order_usd=1, bar_interval="1Day")
    return PaperConfig(**{**base, **kw})


def total_return(equity):
    return float(equity.iloc[-1] / 100_000 - 1)


@pytest.mark.parametrize("engine", ["vectorbt", "backtrader"])
def test_engine_matches_sim_without_costs(engine):
    pytest.importorskip("vectorbt" if engine == "vectorbt" else "backtrader")
    scores, bars = synthetic()
    cfg = config()
    sim_equity, _, sim_orders = simulate(cfg, scores, bars)
    schedule = target_schedule(cfg, scores, pd.DatetimeIndex(sorted(bars["timestamp"].unique())))

    if engine == "vectorbt":
        from src.paper.backtesting import vectorbt_engine as mod
    else:
        from src.paper.backtesting import backtrader_engine as mod
    result = mod.run(cfg, schedule, bars)

    assert result.error is None
    assert len(result.orders) > 0
    assert abs(total_return(result.equity) - total_return(sim_equity)) < 0.005


def test_schedule_ramps_in_and_trades_next_session():
    scores, bars = synthetic()
    cfg = config()
    stamps = pd.DatetimeIndex(sorted(bars["timestamp"].unique()))
    schedule = target_schedule(cfg, scores, stamps)
    first = min(schedule)
    assert first == stamps[1]  # decisions dated day 0 trade on session 1
    gross = [w.abs().sum() for _, w in sorted(schedule.items())]
    assert gross[0] == pytest.approx(0.2)  # 1/5 of 1x gross after one vintage
    assert gross[4] == pytest.approx(1.0, abs=0.35)


def test_write_run_saves_files_and_report(tmp_path):
    from src.paper.backtesting import compare, output
    from src.paper.backtesting.inputs import EngineResult

    scores, bars = synthetic()
    cfg = config()
    sim_equity, _, sim_orders = simulate(cfg, scores, bars)
    bumped = EngineResult("other", sim_equity * 1.02, sim_orders)
    broken = EngineResult("lumibot", error="ImportError: nope")
    results = [EngineResult("sim", sim_equity, sim_orders), bumped, broken]
    bench = bars[bars["symbol"] == "SPY"].set_index("timestamp")["close"]

    run_dir = output.write_run(cfg, results, bench, ["W", "M"], "2026-01-05", "2026-03-06", out_root=tmp_path)

    names = {p.name for p in run_dir.iterdir()}
    assert {"report.html", "report.md", "params.json", "summary.json", "equity_sim.csv", "orders_sim.csv"} <= names
    assert (tmp_path / "latest").resolve() == run_dir.resolve()
    page = (run_dir / "report.html").read_text()
    assert "Skipped engines" in page
    assert "ImportError: nope" in page
    assert "<polyline" in page
    table = compare.agreement(results, cfg.capital).set_index("engine")
    assert bool(table.loc["other", "agrees"]) is False  # +2% scale is far past the tolerance


def test_symbol_spelling_round_trips():
    from src.paper.symbols import from_alpaca, to_alpaca

    assert to_alpaca("BF-B") == "BF.B"
    assert from_alpaca("BRK.B") == "BRK-B"
    assert to_alpaca("AAPL") == "AAPL"


def test_fetch_bars_maps_symbols_and_drops_rejected(monkeypatch, tmp_path):
    import alpaca.data.historical as hist
    from alpaca.common.exceptions import APIError

    from src.paper.backtest import fetch_bars

    asked = []

    class Result:
        def __init__(self, names):
            idx = pd.MultiIndex.from_product([names, pd.to_datetime(["2026-01-05"], utc=True)], names=["symbol", "timestamp"])
            self.df = pd.DataFrame({"open": 1.0, "close": 1.0, "high": 1.0}, index=idx)

    class Client:
        def __init__(self, *a, **k): ...

        def get_stock_bars(self, request):
            names = list(request.symbol_or_symbols)
            asked.append(names)
            if "DEAD" in names:
                raise APIError('{"message":"invalid symbol: DEAD"}')
            return Result(names)

    monkeypatch.setattr(hist, "StockHistoricalDataClient", Client)
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    cfg = config(out_dir=str(tmp_path), bar_interval="1Day")
    bars = fetch_bars(cfg, ["BF-B", "DEAD", "SPY"], "2026-01-01", "2026-02-01")

    assert asked[0] == ["BF.B", "DEAD", "SPY"]
    assert asked[1] == ["BF.B", "SPY"]
    assert sorted(bars["symbol"]) == ["BF-B", "SPY"]


def test_lumibot_child_env_has_no_broker_credentials(monkeypatch):
    from pathlib import Path

    from src.paper.backtesting.lumibot_engine import _child_env

    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("AURUM_PASSWORD", "p")
    monkeypatch.setenv("PATH", "/bin")
    env = _child_env(Path("/repo"))
    assert "ALPACA_API_KEY" not in env
    assert "AURUM_PASSWORD" not in env
    assert env["PATH"] == "/bin"
    assert env["PYTHONPATH"] == "/repo"
    assert env["LUMIBOT_DISABLE_DOTENV"] == "1"
