"""Replay the holdout through SimBroker on Alpaca historical bars.

Same book, order and report code as live. What differs from `inference.replay`: fills
happen at a real intraday bar on the session after each decision (not at the decision
date's close), after `cost_bps`, and the curve is marked at the chosen bar interval.
"""

import asyncio
import hashlib
import logging
import re
from pathlib import Path

import pandas as pd

from src.inference.resolve import resolve_run
from src.modeling.config import load_config
from src.modeling.evaluate.runner import _panel
from src.modeling.models.registry import load_run
from src.paper import book
from src.paper.broker import SimBroker
from src.paper.backtesting.inputs import TZ, target_schedule
from src.paper.config import PaperConfig
from src.paper.symbols import from_alpaca, to_alpaca

logger = logging.getLogger(__name__)


def holdout_scores(config: PaperConfig) -> pd.DataFrame:
    """`date, symbol, score` for every holdout row, scored by the configured run."""
    modeling = load_config(config.model_config)
    directory = asyncio.run(resolve_run(modeling.output_dir, config.model_version, modeling.export))
    booster, manifest = load_run(directory.parent, directory.name)
    frame, matrix = _panel(modeling, manifest)
    holdout = (frame["fold_id"] > modeling.splits.eval_end_fold).to_numpy()
    return pd.DataFrame(
        {
            "date": pd.to_datetime(frame.loc[holdout, "date"]).to_numpy(),
            "symbol": frame.loc[holdout, "symbol"].to_numpy(),
            "score": booster.predict(matrix[holdout]),
        }
    )


def parse_timeframe(text: str):
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    m = re.fullmatch(r"(\d+)(Min|Hour|Day)", text)
    if not m:
        raise ValueError(f"bar_interval must look like 30Min, 1Hour or 1Day, got {text!r}")
    unit = {"Min": TimeFrameUnit.Minute, "Hour": TimeFrameUnit.Hour, "Day": TimeFrameUnit.Day}[m.group(2)]
    return TimeFrame(int(m.group(1)), unit)


def fetch_bars(config: PaperConfig, symbols: list[str], start: str, end: str) -> pd.DataFrame:
    """Split/dividend-adjusted bars, long format `symbol, timestamp, open, close`, cached."""
    key = hashlib.md5(f"{sorted(symbols)}".encode()).hexdigest()[:10]
    cache = Path(config.out_dir) / "bars" / f"{config.bar_interval}_{start}_{end}_{key}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)

    from alpaca.common.exceptions import APIError
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest

    client = StockHistoricalDataClient(*config.credentials())
    frames = []
    dropped: list[str] = []
    pending = [to_alpaca(sym) for sym in symbols]
    for i in range(0, len(pending), 100):
        batch = pending[i : i + 100]
        logger.info("Fetching bars %d-%d of %d", i, i + len(batch), len(pending))
        while batch:
            request = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=parse_timeframe(config.bar_interval),
                start=pd.Timestamp(start, tz=TZ).to_pydatetime(),
                end=pd.Timestamp(end, tz=TZ).to_pydatetime(),
                adjustment=Adjustment.ALL,
                feed=DataFeed(config.feed),
            )
            try:
                frames.append(client.get_stock_bars(request).df[["open", "close"]].reset_index())
                break
            except APIError as error:
                # One symbol Alpaca does not know (delisted, renamed) rejects the whole batch.
                bad = re.search(r"invalid symbol: ([\w.\-]+)", str(error))
                if not bad or bad.group(1) not in batch:
                    raise
                logger.warning("Alpaca rejected %s; dropping it from the universe", bad.group(1))
                dropped.append(from_alpaca(bad.group(1)))
                batch = [b for b in batch if b != bad.group(1)]
    if dropped:
        logger.warning("Dropped %d symbols Alpaca does not carry: %s", len(dropped), ", ".join(dropped))
    bars = pd.concat(frames, ignore_index=True)
    bars["symbol"] = bars["symbol"].map(from_alpaca)
    cache.parent.mkdir(parents=True, exist_ok=True)
    bars.to_parquet(cache)
    return bars


def _wide(bars: pd.DataFrame, column: str) -> pd.DataFrame:
    return bars.pivot(index="timestamp", columns="symbol", values=column).sort_index()


def simulate(
    config: PaperConfig, scores: pd.DataFrame, bars: pd.DataFrame
) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
    """Run the schedule. Returns (equity curve, benchmark close, order log)."""
    opens, closes = _wide(bars, "open"), _wide(bars, "close")
    closes_ff = closes.ffill()
    schedule = target_schedule(config, scores, closes.index)

    broker = SimBroker(config.capital, config.cost_bps)
    curve, orders_log = {}, []
    for ts in closes.index:
        if ts in schedule:
            broker.set_prices(opens.loc[ts].fillna(closes_ff.shift(1).loc[ts]))
            target = schedule[ts]
            symbols = sorted(target.index.union(broker.holdings().index))
            orders = book.build_orders(target, broker.holdings(), broker.prices(symbols), broker.equity(), config.min_order_usd)
            orders_log.append(broker.submit(orders, str(ts.date())).assign(ts=ts))
        broker.set_prices(closes_ff.loc[ts])
        curve[ts] = broker.equity()

    equity = pd.Series(curve, name="equity")
    bench = closes[config.benchmark].dropna() if config.benchmark in closes else pd.Series(dtype="float64")
    log = pd.concat(orders_log, ignore_index=True) if orders_log else pd.DataFrame()
    return equity, bench, log
