"""backtrader: one data feed per symbol, orders placed on the exec bar's open (cheat-on-open)."""

import pandas as pd

from src.paper.backtesting.inputs import EngineResult
from src.paper.config import PaperConfig

MAX_30MIN_SYMBOLS = 50


def _largest_weights(names: list[str], schedule: dict, benchmark: str) -> list[str]:
    """~500 feeds of 30-minute bars take hours in backtrader's per-bar Python loop; keep the
    benchmark plus the names with the largest absolute weights (the report says so)."""
    weight_mass = pd.concat(schedule.values(), axis=1).abs().fillna(0).sum(axis=1)
    keep = set(weight_mass.nlargest(MAX_30MIN_SYMBOLS).index) | {benchmark}
    return [n for n in names if n in keep]


def run(config: PaperConfig, schedule: dict, bars: pd.DataFrame) -> EngineResult:
    import backtrader as bt

    names = sorted(bars["symbol"].unique())
    intraday = not config.bar_interval.endswith("Day")
    note = ""
    if intraday and len(names) > MAX_30MIN_SYMBOLS:
        names = _largest_weights(names, schedule, config.benchmark)
        bars = bars[bars["symbol"].isin(names)]
        note = f"subsampled to {len(names)} symbols (30-minute bars)"
    schedule = {ts: w[w.index.isin(names)] for ts, w in schedule.items()}

    bars = bars.assign(timestamp=bars["timestamp"].dt.tz_convert("UTC").dt.tz_localize(None))
    exec_at = {pd.Timestamp(ts).tz_convert("UTC").tz_localize(None): w for ts, w in schedule.items()}
    curve: dict[pd.Timestamp, float] = {}
    log: list[dict] = []

    class FromSchedule(bt.Strategy):
        def _record(self):
            curve[pd.Timestamp(self.data0.datetime.datetime(0))] = self.broker.getvalue()

        def next_open(self):
            now = pd.Timestamp(self.data0.datetime.datetime(0))
            target = exec_at.get(now)
            if target is None:
                return
            equity = self.broker.getvalue()
            wanted = {}
            for data in self.datas:
                price = data.open[0]
                weight = float(target.get(data._name, 0.0))
                wanted[data] = int(weight * equity / price) if pd.notna(price) and price > 0 else 0
            # Sells first so freed cash is there for the buys.
            ordered = sorted(wanted.items(), key=lambda kv: kv[1] - self.getposition(kv[0]).size)
            for data, shares in ordered:
                delta = shares - self.getposition(data).size
                if delta == 0:
                    continue
                price = data.open[0]
                if abs(delta * price) < config.min_order_usd:
                    continue
                self.order_target_size(data=data, target=shares)
                log.append({"ts": now, "symbol": data._name, "side": "buy" if delta > 0 else "sell", "qty": abs(delta), "price": price})

        def next(self):
            self._record()

    cerebro = bt.Cerebro(stdstats=False, cheat_on_open=True)
    cerebro.broker.set_cash(config.capital)
    cerebro.broker.set_coo(True)
    cerebro.broker.setcommission(commission=config.cost_bps / 1e4)
    # SPY first: it has a bar at every timestamp, so it sets the strategy's clock.
    for name in sorted(names, key=lambda n: n != config.benchmark):
        frame = bars[bars["symbol"] == name].set_index("timestamp").sort_index()[["open", "close"]]
        frame = frame.assign(high=frame[["open", "close"]].max(axis=1), low=frame[["open", "close"]].min(axis=1), volume=0.0)
        cerebro.adddata(bt.feeds.PandasData(dataname=frame, open="open", high="high", low="low", close="close", volume="volume", openinterest=None), name=name)
    cerebro.addstrategy(FromSchedule)
    cerebro.run(runonce=False)

    equity = pd.Series(curve, name="equity").sort_index()
    equity.index = equity.index.tz_localize("UTC")
    orders = pd.DataFrame(log)
    if len(orders):
        orders["ts"] = pd.to_datetime(orders["ts"]).dt.tz_localize("UTC")
    return EngineResult("backtrader", equity, orders, note=(note or "").strip())
