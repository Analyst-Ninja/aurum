"""Line the engines up: headline stats and how far each strays from the in-house sim."""

import numpy as np
import pandas as pd

from src.paper import report
from src.paper.backtesting.inputs import TZ, EngineResult

REFERENCE = "sim"
# Total-return gap (percentage points) beyond which an engine is flagged. Whole-share
# rounding and fill-bar conventions explain fractions of a point; more is a real difference.
TOLERANCE_PP = 0.5


def session_closes(equity: pd.Series) -> pd.Series:
    """Last equity of each ET session, indexed by the session date."""
    idx = equity.index.tz_convert(TZ).normalize().tz_localize(None)
    return equity.groupby(idx).last()


def agreement(results: list[EngineResult], capital: float) -> pd.DataFrame:
    """One row per engine. Gaps are against the `sim` result when it ran."""
    ok = [r for r in results if r.error is None and len(r.equity)]
    ref = next((r for r in ok if r.name == REFERENCE), None)
    ref_daily = session_closes(ref.equity).pct_change() if ref else None
    ref_total = ref.equity.iloc[-1] / capital - 1 if ref else None

    rows = []
    for r in ok:
        s = report.summary(r.equity, capital)
        row = {
            "engine": r.name,
            "total_return": s["total_return"],
            "pnl": s["pnl"],
            "sharpe": s["sharpe"],
            "max_drawdown": s["max_drawdown"],
            "orders": int(len(r.orders)),
        }
        if ref is not None and r.name != REFERENCE:
            both = pd.concat([session_closes(r.equity).pct_change(), ref_daily], axis=1, join="inner").dropna()
            gap_pp = (r.equity.iloc[-1] / capital - 1 - ref_total) * 100
            row["gap_vs_sim_pp"] = gap_pp
            row["max_daily_gap_pp"] = float(np.abs(both.iloc[:, 0] - both.iloc[:, 1]).max() * 100) if len(both) else float("nan")
            row["agrees"] = bool(abs(gap_pp) <= TOLERANCE_PP)
        rows.append(row)
    return pd.DataFrame(rows)
