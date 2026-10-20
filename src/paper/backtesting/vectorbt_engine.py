"""vectorbt: the whole book as one cash-shared group, sized to target percent at each exec bar."""

import numpy as np
import pandas as pd

from src.paper.backtesting.inputs import EngineResult, weight_frame, wide
from src.paper.config import PaperConfig


def run(config: PaperConfig, schedule: dict, bars: pd.DataFrame) -> EngineResult:
    import vectorbt as vbt

    opens, closes = wide(bars, "open"), wide(bars, "close").ffill()
    opens = opens.fillna(closes.shift(1)).fillna(closes)
    weights = weight_frame(schedule)
    if weights.empty:
        return EngineResult("vectorbt", error="empty schedule")
    symbols = sorted(set(weights.columns) & set(closes.columns))
    closes, opens = closes[symbols], opens[symbols]

    # NaN = no order. At an exec bar every symbol gets an explicit target, 0 for names that left.
    size = pd.DataFrame(np.nan, index=closes.index, columns=symbols)
    size.loc[weights.index, symbols] = weights.reindex(columns=symbols).fillna(0.0).to_numpy()

    pf = vbt.Portfolio.from_orders(
        close=closes,
        price=opens,
        size=size,
        size_type="targetpercent",
        size_granularity=1,
        direction="both",
        fees=config.cost_bps / 1e4,
        init_cash=config.capital,
        cash_sharing=True,
        group_by=True,
        call_seq="auto",
        freq=config.bar_interval.replace("Min", "min").replace("Day", "D"),
    )
    records = pf.orders.records_readable
    orders = pd.DataFrame(
        {
            "ts": records["Timestamp"],
            "symbol": records["Column"],
            "side": records["Side"].str.lower(),
            "qty": records["Size"],
            "price": records["Price"],
        }
    )
    return EngineResult("vectorbt", pd.Series(pf.value().squeeze(), name="equity"), orders, note=f"vectorbt {vbt.__version__}")
