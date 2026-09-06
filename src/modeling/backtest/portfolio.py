"""Turn a per-(symbol, date) prediction into a held book.

The construction is the one the 5-day horizon forces. A book formed every day on a
5-day label would be five books' worth of trading, so instead 1/5 of capital is
committed each day and held for five days: five vintages ("tranches") are live at any
time, each rebalancing every fifth day. Daily turnover is ~2/5 of the book — 1/5 out,
1/5 in — rather than the 2x a naive daily rebalance implies, and that factor of five
is the difference between a strategy that survives costs and one that does not.

Ranking uses `metrics.deciles`, which mirrors `mart_features.sql`. The decile
definition must not fork between the warehouse, the evaluation harness and here.
"""

import numpy as np
import pandas as pd

from src.modeling.evaluate.metrics import N_DECILES, deciles


def formation_calendar(dates: pd.Series, horizon: int) -> pd.DataFrame:
    """Every trading date with the tranche it forms, keyed by position in the calendar."""
    calendar = np.sort(dates.unique())
    return pd.DataFrame(
        {
            "date": calendar,
            "tranche": np.arange(len(calendar)) % horizon,
        }
    )


def build_positions(predictions: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Weights of each vintage at formation: `date, tranche, symbol, weight`.

    Dollar-neutral and equal-weight: the long leg sums to +1/(2*horizon) of capital and
    the short leg to -1/(2*horizon), so the five live vintages together hold 1x long
    and 1x short. Only formation rows are emitted; a vintage is held unchanged for
    `horizon` days, so the full daily book is the union of the last `horizon` rows.
    """
    frame = predictions.dropna(subset=["pred"]).copy()
    frame["decile"] = deciles(
        frame["pred"].to_numpy(), frame["date"].to_numpy()
    ).to_numpy()

    legs = frame[frame["decile"].isin([1, N_DECILES])].merge(
        formation_calendar(frame["date"], horizon), on="date", how="left"
    )
    side = np.where(legs["decile"] == N_DECILES, 1.0, -1.0)
    leg_size = legs.groupby(["date", "decile"], observed=True)["symbol"].transform(
        "size"
    )
    legs["weight"] = side / (2.0 * horizon * leg_size)
    return legs[["date", "tranche", "symbol", "weight"]].sort_values(
        ["date", "symbol"], ignore_index=True
    )


def leg_members(predictions: pd.DataFrame) -> pd.DataFrame:
    """`date, symbol, decile` for the two traded deciles only."""
    frame = predictions.dropna(subset=["pred"]).copy()
    frame["decile"] = deciles(
        frame["pred"].to_numpy(), frame["date"].to_numpy()
    ).to_numpy()
    return frame.loc[frame["decile"].isin([1, N_DECILES]), ["date", "symbol", "decile"]]


def rebalance_turnover(predictions: pd.DataFrame, horizon: int) -> pd.Series:
    """Fraction of a vintage replaced at its own rebalance, indexed by formation date.

    Measured against the same tranche's previous formation — `horizon` trading days
    earlier — because that is when a vintage actually trades. Both legs are averaged;
    the first formation of each tranche has nothing to compare against and is dropped.
    """
    members = leg_members(predictions)
    if members.empty:
        return pd.Series(dtype="float64", name="turnover")

    books = {
        (day, decile): set(group["symbol"])
        for (day, decile), group in members.groupby(["date", "decile"], observed=True)
    }
    calendar = np.sort(members["date"].unique())

    rows = {}
    for position in range(horizon, len(calendar)):
        day, previous = calendar[position], calendar[position - horizon]
        churn = [
            1.0 - len(books[(day, decile)] & books[(previous, decile)]) / len(books[(day, decile)])
            for decile in (1, N_DECILES)
            if books.get((day, decile)) and books.get((previous, decile))
        ]
        if churn:
            rows[day] = float(np.mean(churn))
    return pd.Series(rows, name="turnover").sort_index()
