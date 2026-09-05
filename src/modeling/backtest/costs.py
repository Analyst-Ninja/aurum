"""Transaction costs, and the only number that really matters: the break-even.

A single Sharpe at one assumed cost level is a claim about a broker, not about a
signal. "The strategy dies above X bps per side" is checkable and survives a change of
venue, so the sweep is the headline and `break_even_bps` is the summary of it.
"""

import numpy as np
import pandas as pd

BPS = 1e-4


def cost_rate_bps(
    half_spread_bps: float,
    impact_k: float,
    vol_21d: float | np.ndarray,
    participation: float | np.ndarray,
) -> float | np.ndarray:
    """Per-side cost in bps: fixed half-spread plus square-root impact.

    `impact_k` is assumed, not calibrated (`docs/modeling/backtesting.md` §9 gap 4),
    and `participation` is position size as a fraction of `adv_21d`. Reported at a
    nominal participation rather than used in the sweep: the sweep's grid is the
    honest statement, this is what a plausible level looks like.
    """
    return half_spread_bps + impact_k * np.asarray(vol_21d) * np.sqrt(
        np.asarray(participation)
    )


def net_returns(
    spread: pd.Series, turnover: pd.Series, cost_bps: float
) -> pd.Series:
    """Vintage returns net of the round trip that formed them.

    A `turnover` of f means f of the book was sold and f bought, so traded notional is
    2f of gross exposure and the charge is `2 * f * cost_bps`. Formation dates with no
    turnover measurement (the first of each tranche) are charged nothing rather than
    dropped — dropping them would silently shorten the series.
    """
    charge = 2.0 * turnover.reindex(spread.index).fillna(0.0) * cost_bps * BPS
    return (spread - charge).rename("net")


def break_even_bps(
    spread: pd.Series, turnover: pd.Series, ceiling: float = 200.0
) -> float:
    """Per-side cost at which the mean net return reaches zero.

    Closed form rather than a search: the charge is linear in `cost_bps`, so the
    crossing is `mean(spread) / (2 * mean(turnover) * 1e-4)`. Returns 0.0 when the
    gross spread is already negative, and caps at `ceiling` so an implausible number
    from a near-zero turnover does not read as a result.
    """
    aligned = turnover.reindex(spread.index).fillna(0.0)
    gross, churn = float(spread.mean()), float(aligned.mean())
    if not np.isfinite(gross) or gross <= 0.0:
        return 0.0
    if churn <= 0.0:
        return ceiling
    return float(min(gross / (2.0 * churn * BPS), ceiling))
