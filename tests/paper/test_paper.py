import numpy as np
import pandas as pd
import pytest

from src.paper import book, report
from src.paper.backtest import simulate
from src.paper.broker import SimBroker
from src.paper.config import PaperConfig, load_paper_config


def _decisions(buy, sell):
    rows = [(s, "BUY") for s in buy] + [(s, "SELL") for s in sell]
    return pd.DataFrame(rows, columns=["symbol", "action"])


def test_daily_book_is_dollar_neutral():
    w = book.daily_book(_decisions(["A", "B"], ["C", "D"]), leg_exposure=0.5)
    assert w[["A", "B"]].sum() == pytest.approx(0.5)
    assert w[["C", "D"]].sum() == pytest.approx(-0.5)


def test_long_only_drops_shorts():
    w = book.daily_book(_decisions(["A"], ["C"]), long_only=True)
    assert list(w.index) == ["A"]


def test_target_ramps_in_over_horizon():
    one = book.daily_book(_decisions(["A"], ["B"]), 0.5)
    t1 = book.target_weights([one], horizon=5)
    assert t1["A"] == pytest.approx(0.1)  # 1/5 of the leg after one day
    t5 = book.target_weights([one] * 5, horizon=5)
    assert t5["A"] == pytest.approx(0.5)


def test_target_drops_name_that_leaves_window():
    a = pd.Series({"A": 0.5})
    b = pd.Series({"B": 0.5})
    t = book.target_weights([a, b, b], horizon=2)
    assert "A" not in t.index or t["A"] == 0


def test_build_orders_truncates_and_skips_small():
    target = pd.Series({"A": 0.5, "B": -0.5, "C": 0.0001})
    prices = pd.Series({"A": 10.0, "B": 20.0, "C": 10.0})
    orders = book.build_orders(target, pd.Series(dtype="float64"), prices, 1000.0, min_order_usd=50)
    got = orders.set_index("symbol")
    assert got.loc["A", "side"] == "buy"
    assert got.loc["A", "qty"] == 50
    assert got.loc["B", "side"] == "sell"
    assert got.loc["B", "qty"] == 25
    assert "C" not in got.index


def test_build_orders_closes_dropped_position():
    orders = book.build_orders(
        pd.Series(dtype="float64"), pd.Series({"A": 10.0}), pd.Series({"A": 20.0}), 1000.0
    )
    assert orders.iloc[0].to_dict() == {"symbol": "A", "side": "sell", "qty": 10.0, "price": 20.0}


def test_simbroker_round_trip_costs():
    broker = SimBroker(1000.0, cost_bps=100)
    broker.set_prices(pd.Series({"A": 10.0}))
    broker.submit(pd.DataFrame({"symbol": ["A"], "side": ["buy"], "qty": [10.0], "price": [10.0]}), "d")
    assert broker.holdings()["A"] == 10
    assert broker.equity() == pytest.approx(1000.0 - 1.0)  # 1% of 100 paid on entry


def _equity(values, start="2026-01-02"):
    idx = pd.date_range(start, periods=len(values), freq="B", tz="UTC")
    return pd.Series(values, index=idx, dtype="float64")


def test_period_table_chains_bases():
    eq = _equity([100, 101, 102, 103, 104, 110, 108, 112, 111, 115], "2026-01-05")
    weekly = report.period_table(eq, "W", start_equity=100)
    assert weekly["start"].iloc[0] == 100
    assert weekly["start"].iloc[1] == weekly["end"].iloc[0]
    assert weekly["pnl"].sum() == pytest.approx(eq.iloc[-1] - 100)


def test_period_rejects_unknown_code():
    equity = _equity([1, 2, 3])
    with pytest.raises(ValueError):
        report.period_table(equity, "X")


def test_summary_drawdown():
    s = report.summary(_equity([100, 120, 90, 95]), start_equity=100)
    assert s["max_drawdown"] == pytest.approx(-0.25)
    assert s["total_return"] == pytest.approx(-0.05)


def test_config_rejects_unknown_key(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("capital: 5\nbogus: 1\n")
    path = str(f)
    with pytest.raises(ValueError):
        load_paper_config(path)


def test_simulate_trades_next_session_and_marks_every_bar():
    # Two sessions of 2 bars each (ET 09:30, 10:00). Decisions dated the day before session 1.
    idx = pd.DatetimeIndex(
        [
            "2026-03-03 14:30", "2026-03-03 15:00",  # 09:30/10:00 ET (EST -> UTC+5)... March 3 is EST
            "2026-03-04 14:30", "2026-03-04 15:00",
        ],
        tz="UTC",
    )
    fillers = {f"S{i}": [10] * 4 for i in range(18)}
    rows = []
    for sym, opens in {"UP": [10, 10, 11, 12], "DN": [10, 10, 9, 8], "SPY": [100] * 4, **fillers}.items():
        for ts, o in zip(idx, opens):
            rows.append((sym, ts, float(o), float(o)))
    bars = pd.DataFrame(rows, columns=["symbol", "timestamp", "open", "close"])
    names = ["UP", "DN", *fillers]
    scores = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-03-02"] * len(names)),
            "symbol": names,
            "score": [9.0, -9.0, *np.linspace(-1, 1, 18)],
        }
    )
    cfg = PaperConfig(capital=10_000, cost_bps=0, horizon=1, leg_exposure=0.5, min_order_usd=1, execution_time="09:30")
    equity, bench, orders = simulate(cfg, scores, bars)
    assert len(equity) == 4
    assert {"UP", "DN"} <= set(orders["symbol"])
    assert equity.iloc[-1] > 10_000  # long UP rose, short DN fell
    assert equity.iloc[0] == pytest.approx(10_000)  # filled at the open, marked at same price
