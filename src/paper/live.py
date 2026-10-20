"""Live paper trading: score, rebalance toward the 5-book target, mark to market."""

import asyncio
import logging
import time

import pandas as pd

from src.inference.run import decide_day, make_engine
from src.modeling.config import load_config
from src.paper import book
from src.paper.broker import AlpacaBroker
from src.paper.config import PaperConfig
from src.paper.ledger import Ledger

logger = logging.getLogger(__name__)


async def _decide(config: PaperConfig, asof: str | None):
    modeling = load_config(config.model_config)
    engine = make_engine(modeling.source)
    try:
        return await decide_day(engine, modeling, config.model_version, asof)
    finally:
        await engine.dispose()


def _books(ledger: Ledger, asof: str, config: PaperConfig) -> tuple[list[pd.Series], int]:
    """Vintages for the `horizon` newest decision days up to and including `asof`."""
    past = ledger.read("decisions")
    if past.empty:
        return [], 0
    past = past[past["asof"] <= asof]
    days = sorted(past["asof"].unique())
    books = [
        book.daily_book(past[past["asof"] == d], config.leg_exposure, config.long_only)
        for d in days[-config.horizon :]
    ]
    return books, len(days)


def rebalance(config: PaperConfig, asof: str | None = None, dry_run: bool = False, force: bool = False) -> pd.DataFrame:
    broker = AlpacaBroker(config)
    ledger = Ledger(config.ledger_url)
    if not force and not broker.is_open():
        raise RuntimeError("Market is closed; use --force to run anyway")

    result, meta = asyncio.run(_decide(config, asof))
    day = str(meta["asof"])
    if ledger.decided(day):
        logger.info("Already decided %s; nothing to do", day)
        return pd.DataFrame()

    legs = result[result["action"].isin([book.BUY, book.SELL])]
    stored = legs[["symbol", "action", "score"]].assign(asof=day, version=meta["version"])
    if not dry_run:
        ledger.append("decisions", stored)
    books, n_days = _books(ledger, day, config)
    if dry_run:
        books = (books + [book.daily_book(legs, config.leg_exposure, config.long_only)])[-config.horizon :]
        n_days += 1
    if n_days % config.rebalance_every_n_days:
        logger.info("Day %d of every-%d cadence; recorded decisions, no trades", n_days, config.rebalance_every_n_days)
        return pd.DataFrame()

    target = book.target_weights(books, config.horizon)
    holdings = broker.holdings()
    symbols = sorted(target.index.union(holdings.index))
    orders = book.build_orders(target, holdings, broker.prices(symbols), broker.equity(), config.min_order_usd)
    print(f"{day}: target {len(target)} names, {len(orders)} orders ({(orders['side'] == 'buy').sum()} buy / {(orders['side'] == 'sell').sum()} sell)")
    if dry_run:
        print(orders.to_string(index=False))
        return orders

    sent = broker.submit(orders, day)
    ledger.append("orders", sent.assign(asof=day))
    snapshot(config, broker, ledger)
    return sent


def snapshot(config: PaperConfig, broker: AlpacaBroker | None = None, ledger: Ledger | None = None) -> dict:
    broker = broker or AlpacaBroker(config)
    ledger = ledger or Ledger(config.ledger_url)
    holdings = broker.holdings()
    prices = broker.prices(sorted({*holdings.index, config.benchmark}))
    value = holdings * prices.reindex(holdings.index)
    row = {
        "ts": pd.Timestamp.now(tz="UTC").isoformat(),
        "equity": broker.equity(),
        "cash": broker.cash(),
        "gross": float(value.abs().sum()),
        "net": float(value.sum()),
        "benchmark": float(prices.get(config.benchmark, float("nan"))),
    }
    ledger.append("snapshots", pd.DataFrame([row]))
    return row


def watch(config: PaperConfig) -> None:
    """Snapshot every `snapshot_interval_min` while the market is open, then return."""
    broker = AlpacaBroker(config)
    ledger = Ledger(config.ledger_url)
    while broker.is_open():
        row = snapshot(config, broker, ledger)
        logger.info("equity %.2f", row["equity"])
        time.sleep(config.snapshot_interval_min * 60)
