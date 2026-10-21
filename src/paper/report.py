"""Equity curve -> weekly / monthly / quarterly / yearly P&L tables."""

import numpy as np
import pandas as pd

FREQ = {"W": "W-FRI", "M": "ME", "Q": "QE", "Y": "YE"}
NAME = {"W": "Weekly", "M": "Monthly", "Q": "Quarterly", "Y": "Yearly"}
TRADING_DAYS = 252


def _daily(equity: pd.Series) -> pd.Series:
    return equity.groupby(equity.index.normalize()).last()


def period_table(equity: pd.Series, code: str, start_equity: float | None = None) -> pd.DataFrame:
    """P&L per calendar period. A period's base is the previous period's last equity.

    The first period is based on `start_equity` (the capital) when given, otherwise on its
    own first observation. The last period may be partial and is marked so.
    """
    if code not in FREQ:
        raise ValueError(f"period must be one of {sorted(FREQ)}, got {code!r}")
    equity = equity.sort_index()
    ends = equity.resample(FREQ[code]).last().dropna()
    base = ends.shift(1)
    base.iloc[0] = start_equity if start_equity is not None else equity.iloc[0]
    table = pd.DataFrame({"start": base, "end": ends})
    table["pnl"] = table["end"] - table["start"]
    table["return"] = table["end"] / table["start"] - 1.0
    table["partial"] = table.index.normalize() > equity.index[-1].normalize()
    return table


def summary(equity: pd.Series, start_equity: float | None = None) -> dict:
    """Whole-run totals: return, annualised Sharpe on daily closes, max drawdown."""
    daily = _daily(equity.sort_index())
    base = start_equity if start_equity is not None else daily.iloc[0]
    rets = daily.pct_change().dropna()
    if start_equity is not None and len(daily):
        rets = pd.concat([pd.Series([daily.iloc[0] / base - 1.0], index=[daily.index[0]]), rets])
    peak = daily.cummax()
    return {
        "days": int(len(daily)),
        "total_return": float(daily.iloc[-1] / base - 1.0),
        "pnl": float(daily.iloc[-1] - base),
        "sharpe": float(rets.mean() / rets.std(ddof=1) * np.sqrt(TRADING_DAYS)) if len(rets) > 2 and rets.std() > 0 else float("nan"),
        "max_drawdown": float((daily / peak - 1.0).min()),
    }


def render(equity: pd.Series, periods: list[str], start_equity: float | None, benchmark: pd.Series | None = None) -> str:
    """Plain-text report: totals, then one table per requested period."""
    lines = []
    s = summary(equity, start_equity)
    lines.append(
        f"{s['days']} days  return {s['total_return']:+.2%}  P&L ${s['pnl']:+,.0f}  "
        f"Sharpe {s['sharpe']:.2f}  max DD {s['max_drawdown']:.2%}"
    )
    if benchmark is not None and len(benchmark.dropna()) > 1:
        b = benchmark.dropna().sort_index()
        lines.append(f"benchmark total return {b.iloc[-1] / b.iloc[0] - 1.0:+.2%}")
    for code in periods:
        table = period_table(equity, code, start_equity)
        if benchmark is not None and len(benchmark.dropna()) > 1:
            bench = period_table(benchmark.dropna(), code)["return"].reindex(table.index)
            table["bench"] = bench
        lines.append(f"\n{NAME[code]}")
        shown = table.assign(
            period=table.index.strftime("%Y-%m-%d"),
            start=table["start"].map("{:,.0f}".format),
            end=table["end"].map("{:,.0f}".format),
            pnl=table["pnl"].map("{:+,.0f}".format),
            ret=table["return"].map("{:+.2%}".format),
            part=table["partial"].map({True: "partial", False: ""}),
        )
        cols = ["period", "start", "end", "pnl", "ret"] + (["bench"] if "bench" in table else []) + ["part"]
        if "bench" in shown:
            shown["bench"] = shown["bench"].map(lambda v: "" if pd.isna(v) else f"{v:+.2%}")
        lines.append(shown[cols].to_string(index=False))
    return "\n".join(lines)
