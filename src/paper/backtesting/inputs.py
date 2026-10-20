"""Inputs every engine shares: bars, and the target weights to hold from each execution bar.

Engines are compared on how they size, fill, charge and mark orders, not on how the book is
built, so one schedule feeds all of them. The schedule is the same one `paper.backtest`
trades from: the book of each decision day is one vintage, the target is the mean of the
newest `horizon` vintages, and it is set at the first bar past `execution_time` of the
session after the decision.
"""

from dataclasses import dataclass, field

import pandas as pd

from src.inference import decide as decisions
from src.paper import book
from src.paper.config import PaperConfig

TZ = "America/New_York"


@dataclass
class EngineResult:
    """One engine's output. `error` set means it did not run; `note` records caveats."""

    name: str
    equity: pd.Series = field(default_factory=lambda: pd.Series(dtype="float64"))
    orders: pd.DataFrame = field(default_factory=pd.DataFrame)
    error: str | None = None
    note: str = ""


def wide(bars: pd.DataFrame, column: str) -> pd.DataFrame:
    return bars.pivot(index="timestamp", columns="symbol", values=column).sort_index()


def target_schedule(
    config: PaperConfig, scores: pd.DataFrame, stamps: pd.DatetimeIndex
) -> dict[pd.Timestamp, pd.Series]:
    """Execution bar -> target weights (symbol -> fraction of equity)."""
    acted = decisions.assign_actions(scores)
    acted["date"] = pd.to_datetime(acted["date"])
    by_day = {d: g for d, g in acted.groupby("date") if g["action"].isin([book.BUY, book.SELL]).any()}
    decision_days = sorted(by_day)

    local = stamps.tz_convert(TZ)
    sessions = pd.Series(local.normalize().tz_localize(None), index=stamps)
    schedule: dict[pd.Timestamp, pd.Series] = {}
    books: list[pd.Series] = []
    used: set[pd.Timestamp] = set()
    n_decided = 0
    for day, group in sessions.groupby(sessions):
        prior = [d for d in decision_days if d < day]
        asof = prior[-1] if prior else None
        if asof is None or asof in used:
            continue
        used.add(asof)
        books.append(book.daily_book(by_day[asof], config.leg_exposure, config.long_only))
        n_decided += 1
        if n_decided % config.rebalance_every_n_days:
            continue
        times = group.index
        exec_at = next((t for t in times if t.tz_convert(TZ).strftime("%H:%M") >= config.execution_time), times[0])
        schedule[exec_at] = book.target_weights(books, config.horizon)
    return schedule


def weight_frame(schedule: dict[pd.Timestamp, pd.Series]) -> pd.DataFrame:
    """Schedule as exec bar x symbol, zeros where a name left the target."""
    if not schedule:
        return pd.DataFrame()
    return pd.DataFrame(schedule).T.sort_index().fillna(0.0)
