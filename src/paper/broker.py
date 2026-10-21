"""Broker interface with two implementations: Alpaca paper (live) and a simulator.

`rebalance` and `backtest` share `book.py` and `report.py`; only the broker differs, so a
backtest exercises the same order maths a live run does.
"""

import logging
from typing import Protocol

import pandas as pd

from src.paper.config import PaperConfig
from src.paper.symbols import from_alpaca, to_alpaca

logger = logging.getLogger(__name__)


class Broker(Protocol):
    def equity(self) -> float: ...
    def cash(self) -> float: ...
    def holdings(self) -> pd.Series: ...
    def prices(self, symbols: list[str]) -> pd.Series: ...
    def submit(self, orders: pd.DataFrame, asof: str) -> pd.DataFrame: ...


class AlpacaBroker:
    """Alpaca paper account. Orders are idempotent per (asof, symbol) via client_order_id."""

    def __init__(self, config: PaperConfig):
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        key, secret = config.credentials()
        self.feed = config.feed
        self.trading = TradingClient(key, secret, paper=True)
        self.data = StockHistoricalDataClient(key, secret)

    def is_open(self) -> bool:
        return bool(self.trading.get_clock().is_open)

    def equity(self) -> float:
        return float(self.trading.get_account().equity)

    def cash(self) -> float:
        return float(self.trading.get_account().cash)

    def holdings(self) -> pd.Series:
        rows = self.trading.get_all_positions()
        return pd.Series({from_alpaca(p.symbol): float(p.qty) for p in rows}, dtype="float64")

    def prices(self, symbols: list[str]) -> pd.Series:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest

        feed = DataFeed(self.feed)
        out: dict[str, float] = {}
        for i in range(0, len(symbols), 200):
            batch = [to_alpaca(sym) for sym in symbols[i : i + 200]]
            trades = self.data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=batch, feed=feed))
            out.update({from_alpaca(s): float(t.price) for s, t in trades.items()})
        return pd.Series(out, dtype="float64")

    def submit(self, orders: pd.DataFrame, asof: str) -> pd.DataFrame:
        from alpaca.common.exceptions import APIError
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        status = []
        for row in orders.itertuples():
            request = MarketOrderRequest(
                symbol=to_alpaca(row.symbol),
                qty=int(row.qty),
                side=OrderSide(row.side),
                time_in_force=TimeInForce.DAY,
                client_order_id=f"{asof}-{row.symbol}",
            )
            try:
                self.trading.submit_order(request)
                status.append("submitted")
            except APIError as error:
                # A repeat run of the same day collides on client_order_id; anything else is real.
                duplicate = "client_order_id" in str(error)
                logger.warning("%s %s: %s", row.symbol, "duplicate" if duplicate else "rejected", error)
                status.append("duplicate" if duplicate else "rejected")
        return orders.assign(status=status)


class SimBroker:
    """Fills at a supplied price plus `cost_bps`. Shorting is unconstrained, no borrow cost."""

    def __init__(self, capital: float, cost_bps: float = 5.0):
        self._cash = float(capital)
        self._qty: dict[str, float] = {}
        self.cost = cost_bps / 1e4
        self.mark: pd.Series = pd.Series(dtype="float64")

    def set_prices(self, marks: pd.Series) -> None:
        self.mark = marks

    def equity(self) -> float:
        held = self.holdings()
        return self._cash + float((held * self.mark.reindex(held.index).fillna(0.0)).sum())

    def cash(self) -> float:
        return self._cash

    def holdings(self) -> pd.Series:
        return pd.Series(self._qty, dtype="float64")

    def prices(self, symbols: list[str]) -> pd.Series:
        return self.mark.reindex(symbols)

    def submit(self, orders: pd.DataFrame, _asof: str) -> pd.DataFrame:
        for row in orders.itertuples():
            sign = 1.0 if row.side == "buy" else -1.0
            fill = row.price * (1 + sign * self.cost)
            self._cash -= sign * row.qty * fill
            qty = self._qty.get(row.symbol, 0.0) + sign * row.qty
            if qty:
                self._qty[row.symbol] = qty
            else:
                self._qty.pop(row.symbol, None)
        return orders.assign(status="filled")
