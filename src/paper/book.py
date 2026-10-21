"""Decisions -> target weights -> orders. Pure pandas, no broker, no I/O.

The target mirrors `modeling.backtest.portfolio`: each day's decile-10 / decile-1 book is
one vintage, and the held portfolio is the average of the last `horizon` vintages. Dividing
by `horizon` even while fewer exist is what ramps the book in at 1/5 a day, like the
tranche scheme the replay validated.
"""

import pandas as pd

BUY, SELL = "BUY", "SELL"


def daily_book(decisions: pd.DataFrame, leg_exposure: float = 0.5, long_only: bool = False) -> pd.Series:
    """One day's vintage: symbol -> weight, equal within each leg."""
    longs = decisions.loc[decisions["action"] == BUY, "symbol"]
    shorts = decisions.loc[decisions["action"] == SELL, "symbol"]
    weights = {s: leg_exposure / len(longs) for s in longs}
    if not long_only:
        weights.update({s: -leg_exposure / len(shorts) for s in shorts})
    return pd.Series(weights, dtype="float64")


def target_weights(books: list[pd.Series], horizon: int) -> pd.Series:
    """Average of the newest `horizon` vintages; absent names count as zero."""
    recent = books[-horizon:]
    if not recent:
        return pd.Series(dtype="float64")
    return pd.concat(recent, axis=1).fillna(0.0).sum(axis=1) / horizon


def build_orders(
    target: pd.Series,
    holdings: pd.Series,
    prices: pd.Series,
    equity: float,
    min_order_usd: float = 50.0,
) -> pd.DataFrame:
    """Share deltas that move `holdings` to `target`: `symbol, side, qty, price`.

    Whole shares only (shorts cannot be fractional), truncated toward zero so the book
    never exceeds its target. A symbol with no price is skipped rather than guessed.
    """
    symbols = target.index.union(holdings.index)
    want = (target.reindex(symbols).fillna(0.0) * equity / prices.reindex(symbols)).dropna()
    want = want.map(lambda q: float(int(q)))
    delta = want - holdings.reindex(want.index).fillna(0.0)
    price = prices.reindex(want.index)
    keep = (delta != 0) & ((delta * price).abs() >= min_order_usd)
    out = pd.DataFrame({"qty": delta[keep].abs(), "price": price[keep]})
    out["side"] = ["buy" if d > 0 else "sell" for d in delta[keep]]
    out = out.rename_axis("symbol").reset_index()
    return out[["symbol", "side", "qty", "price"]].sort_values("symbol", ignore_index=True)
