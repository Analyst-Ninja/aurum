import pandas as pd
import pytest

from src.paper import book, live
from src.paper.config import PaperConfig
from src.paper.ledger import Ledger

DAY = "2026-01-05"


class FakeBroker:
    def __init__(self, config=None, open_states=(True,)):
        self.open_states = list(open_states)
        self.submitted = []

    def is_open(self):
        return self.open_states.pop(0) if len(self.open_states) > 1 else self.open_states[0]

    def equity(self):
        return 100_000.0

    def cash(self):
        return 50_000.0

    def holdings(self):
        return pd.Series({"HELD": 10.0}, dtype="float64")

    def prices(self, symbols):
        return pd.Series(10.0, index=list(symbols))

    def submit(self, orders, asof):
        self.submitted.append((asof, orders))
        return orders.assign(status="filled")


def decisions(day=DAY):
    result = pd.DataFrame(
        {
            "symbol": ["A", "B", "C", "D"],
            "action": [book.BUY, book.BUY, book.SELL, book.SELL],
            "score": [2.0, 1.0, -1.0, -2.0],
        }
    )
    return result, {"asof": day, "version": "v1"}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    config = PaperConfig(ledger_url=f"sqlite:///{tmp_path}/ledger.db", min_order_usd=1, horizon=5)
    broker = FakeBroker()

    async def fake_decide(cfg, asof):
        return decisions(asof or DAY)

    monkeypatch.setattr(live, "AlpacaBroker", lambda cfg: broker)
    monkeypatch.setattr(live, "_decide", fake_decide)
    return config, broker, Ledger(config.ledger_url)


def test_books_empty_without_history(setup):
    config, _, ledger = setup

    assert live._books(ledger, DAY, config) == ([], 0)


def test_books_keep_only_days_up_to_asof(setup):
    config, _, ledger = setup
    for day in ("2026-01-05", "2026-01-06", "2026-01-07"):
        stored = decisions(day)[0].assign(asof=day, version="v1")
        ledger.append("decisions", stored)

    books, n_days = live._books(ledger, "2026-01-06", config)

    assert n_days == 2
    assert len(books) == 2


def test_rebalance_refuses_closed_market(setup):
    config, broker, _ = setup
    broker.open_states = [False]

    with pytest.raises(RuntimeError, match="Market is closed"):
        live.rebalance(config)


def test_rebalance_force_ignores_closed_market(setup):
    config, broker, _ = setup
    broker.open_states = [False]

    sent = live.rebalance(config, force=True)

    assert len(sent) > 0


def test_rebalance_trades_records_and_snapshots(setup):
    config, broker, ledger = setup

    sent = live.rebalance(config)

    assert broker.submitted[0][0] == DAY
    assert set(sent["symbol"]) >= {"A", "B", "C", "D", "HELD"}
    assert ledger.decided(DAY) is True
    assert len(ledger.read("orders")) == len(sent)
    assert len(ledger.read("snapshots")) == 1


def test_rebalance_is_idempotent_per_day(setup):
    config, broker, _ = setup
    live.rebalance(config)
    broker.submitted.clear()

    again = live.rebalance(config)

    assert again.empty
    assert broker.submitted == []


def test_dry_run_prints_orders_and_writes_nothing(setup, capsys):
    config, broker, ledger = setup

    orders = live.rebalance(config, dry_run=True)

    assert len(orders) > 0
    assert broker.submitted == []
    assert ledger.decided(DAY) is False
    assert "orders" in capsys.readouterr().out


def test_off_cadence_day_records_decisions_without_trading(setup):
    config, broker, ledger = setup
    config.rebalance_every_n_days = 2

    sent = live.rebalance(config)

    assert sent.empty
    assert broker.submitted == []
    assert ledger.decided(DAY) is True


def test_snapshot_records_exposure_and_benchmark(setup):
    config, broker, ledger = setup

    row = live.snapshot(config, broker, ledger)

    assert row["equity"] == 100_000.0
    assert row["gross"] == pytest.approx(100.0)
    assert row["benchmark"] == 10.0
    assert len(ledger.read("snapshots")) == 1


def test_watch_snapshots_until_market_closes(setup, monkeypatch):
    config, broker, ledger = setup
    broker.open_states = [True, True, False]
    sleeps = []
    monkeypatch.setattr(live.time, "sleep", sleeps.append)

    live.watch(config)

    assert len(ledger.read("snapshots")) == 2
    assert sleeps == [config.snapshot_interval_min * 60] * 2
