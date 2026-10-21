"""Lumibot, run in a child process on daily bars.

Lumibot needs `mcp<2` while `src/mcp` is written against mcp 2.x, so the two cannot share an
environment. The parent therefore writes the shared inputs to a temp directory and runs
this module under `uv run --with lumibot --with "mcp<2"`, which overlays a private
environment for the one call and leaves the project's lock alone.

Lumibot's backtester is daily-or-minute, so intraday bars are collapsed to one bar per
session. An order submitted in the iteration dated D is filled at D's open, so targets are
submitted on their exec session and sized at that session's open, as the sim does. The curve
is marked at the iteration, before that day's fills, so it can differ from the others by under
1 pp of total return over a year.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

from src.paper.backtesting.inputs import TZ, EngineResult, weight_frame

TIMEOUT_S = 1800
EQUITY_FILE = "equity.csv"
ORDERS_FILE = "orders.csv"
# Lumibot builds a live broker from any credentials it finds in the environment and fails
# on a half-matching set (ALPACA_API_KEY without its own ALPACA_API_SECRET). The child
# trades offline on supplied bars, so it gets none.
CREDENTIAL_PREFIXES = ("ALPACA_", "APCA_", "POLYGON_", "TRADIER_", "TRADOVATE_", "IB_", "SCHWAB_", "AURUM_", "SEC_")


def _child_env(repo: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(CREDENTIAL_PREFIXES)}
    env["PYTHONPATH"] = str(repo)
    # Lumibot also walks up from the running module looking for a .env, which finds the repo's.
    env["LUMIBOT_DISABLE_DOTENV"] = "1"
    return env


def run(config, schedule: dict, bars: pd.DataFrame) -> EngineResult:
    with tempfile.TemporaryDirectory(prefix="aurum-lumibot-") as tmp:
        root = Path(tmp)
        bars.to_parquet(root / "bars.parquet")
        weight_frame(schedule).to_parquet(root / "weights.parquet")
        (root / "params.json").write_text(
            json.dumps({"capital": config.capital, "cost_bps": config.cost_bps, "min_order_usd": config.min_order_usd, "benchmark": config.benchmark})
        )
        repo = Path(__file__).resolve().parents[3]
        # cwd is the temp dir so Lumibot's own log/provenance files do not land in the repo.
        command = [
            "uv", "run", "--project", str(repo), "--group", "backtesting", "--with", "lumibot", "--with", "mcp<2",
            "python", "-m", "src.paper.backtesting.lumibot_engine", str(root),
        ]
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_S, cwd=root, env=_child_env(repo))
        except (subprocess.TimeoutExpired, FileNotFoundError) as error:
            return EngineResult("lumibot", error=f"{type(error).__name__}: {error}")
        if done.returncode != 0 or not (root / EQUITY_FILE).exists():
            tail = "\n".join((done.stderr or done.stdout).strip().splitlines()[-8:])
            return EngineResult("lumibot", error=f"child exited {done.returncode}: {tail}")
        equity = pd.read_csv(root / EQUITY_FILE, index_col=0, parse_dates=True).squeeze("columns")
        equity.index = pd.to_datetime(equity.index, utc=True)
        orders = pd.read_csv(root / ORDERS_FILE) if (root / ORDERS_FILE).exists() else pd.DataFrame()
        return EngineResult("lumibot", equity.rename("equity"), orders, note="daily bars; filled at the open of the submit session")


def _daily(bars: pd.DataFrame, weights: pd.DataFrame) -> pd.DataFrame:
    local = bars["timestamp"].dt.tz_convert(TZ)
    session = local.dt.normalize()
    frame = bars.assign(session=session).sort_values("timestamp")
    exec_open = {ts: ts for ts in weights.index}
    first = frame.groupby(["symbol", "session"]).first()
    last = frame.groupby(["symbol", "session"]).last()
    daily = pd.DataFrame({"open": first["open"], "close": last["close"]}).reset_index()
    daily["high"] = daily[["open", "close"]].max(axis=1)
    daily["low"] = daily[["open", "close"]].min(axis=1)
    daily["volume"] = 1e9
    del exec_open
    return daily


def main(root: Path) -> None:
    from lumibot.backtesting import PandasDataBacktesting
    from lumibot.entities import Asset, Data, TradingFee
    from lumibot.strategies import Strategy

    params = json.loads((root / "params.json").read_text())
    bars = pd.read_parquet(root / "bars.parquet")
    weights = pd.read_parquet(root / "weights.parquet")
    daily = _daily(bars, weights)

    sessions = sorted(daily["session"].unique())
    # Keyed by the exec session itself. The iteration dated D prices at D's close but the
    # market order is filled at D's *open* (the fill event is stamped D+1), so submitting on
    # the exec session reproduces an open fill that day. Submitting a session earlier, as this
    # once did, fills a full day before the decision and turned +17.5% into -5.8% (the model's
    # score is built from data through D-1's close, and D's own move runs against it).
    submit_on = {}
    for ts, row in weights.iterrows():
        day = pd.Timestamp(ts).tz_convert(TZ).normalize()
        if day in sessions:
            submit_on[day] = row[row != 0]
    open_px = daily.set_index(["symbol", "session"])["open"].to_dict()
    curve: dict[pd.Timestamp, float] = {}
    log: list[dict] = []

    pandas_data = {}
    for symbol, frame in daily.groupby("symbol"):
        data = frame.set_index("session")[["open", "high", "low", "close", "volume"]]
        data.index = pd.DatetimeIndex(data.index).tz_convert(TZ)
        pandas_data[Asset(symbol)] = Data(Asset(symbol), data, timestep="day")

    class FromSchedule(Strategy):
        def initialize(self):
            self.sleeptime = "1D"
            self.set_market("24/7")

        def on_trading_iteration(self):
            now = pd.Timestamp(self.get_datetime()).tz_convert(TZ).normalize()
            # Marked at the iteration, i.e. before today's pending fills; the curve runs a day
            # behind the other engines' end-of-bar marks.
            curve[now.tz_convert("UTC")] = self.get_portfolio_value()
            target = submit_on.get(now)
            if target is None:
                return
            equity = self.get_portfolio_value()
            wanted = {}
            for symbol in set(target.index) | {p.asset.symbol for p in self.get_positions() if p.asset.symbol in pandas_names}:
                # Size at the price the order will fill at (the session's open), like the sim.
                # Sizing at the close instead cost ~3 pp of total return over a year.
                price = open_px.get((symbol, now))
                if price and price > 0:
                    wanted[symbol] = (int(float(target.get(symbol, 0.0)) * equity / price), price)
            held = {p.asset.symbol: float(p.quantity) for p in self.get_positions()}
            for symbol, (shares, price) in sorted(wanted.items(), key=lambda kv: kv[1][0] - held.get(kv[0], 0.0)):
                delta = shares - held.get(symbol, 0.0)
                if delta == 0 or abs(delta * price) < params["min_order_usd"]:
                    continue
                side = "buy" if delta > 0 else "sell"
                self.submit_order(self.create_order(Asset(symbol), abs(delta), side))
                log.append({"ts": str(now), "symbol": symbol, "side": side, "qty": abs(delta), "price": price})

    pandas_names = {a.symbol for a in pandas_data}
    start = pd.Timestamp(sessions[0]).tz_localize(None).to_pydatetime()
    end = pd.Timestamp(sessions[-1]).tz_localize(None).to_pydatetime()
    FromSchedule.run_backtest(
        PandasDataBacktesting,
        start,
        end,
        pandas_data=pandas_data,
        benchmark_asset=Asset(params["benchmark"]),
        buy_trading_fees=[TradingFee(percent_fee=params["cost_bps"] / 1e4)],
        sell_trading_fees=[TradingFee(percent_fee=params["cost_bps"] / 1e4)],
        budget=params["capital"],
        analyze_backtest=False,
        show_plot=False,
        show_tearsheet=False,
        save_tearsheet=False,
        save_logfile=False,
        show_progress_bar=False,
        quiet_logs=True,
        save_stats_file=False,
    )
    pd.Series(curve, name="equity").sort_index().to_csv(root / EQUITY_FILE)
    pd.DataFrame(log).to_csv(root / ORDERS_FILE, index=False)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
