"""Return-native metrics for a cross-sectional model.

RMSE on `fwd_ret_5d_excess` is dominated by the tails and says nothing about whether
the ordering is tradeable, so `docs/modeling/modeling-design.md` §5 replaces it with
this set: rank correlation (IC, ICIR), decile spread, and the long-short portfolio the
spread implies (Sharpe, drawdown, turnover, hit rate).

Everything here is a pure function over arrays — no config, no I/O, no model. #56's
backtester imports `long_short_spread` and `tranche_returns` rather than building a
second overlapping-portfolio construction that drifts from this one.

Two things every caller must respect:

* **Labels overlap.** `fwd_ret_5d` on daily bars means five consecutive rows share four
  days of label, so 2.9M rows are not 2.9M observations. Confidence intervals are
  computed against `effective_n`, never the raw count.
* **NaNs are data.** A missing prediction or a missing return drops that row from that
  date rather than becoming a zero, which would be a real (wrong) ranking.
"""

import numpy as np
import pandas as pd

# Trading days per year, for annualizing a Sharpe or an ICIR.
TRADING_DAYS = 252
# Matches the ten buckets GOLD builds in `mart_features.sql`, labelled 1-10.
N_DECILES = 10


def ic_by_date(
    y_true: np.ndarray, y_pred: np.ndarray, dates: np.ndarray
) -> pd.Series:
    """Spearman rank correlation within each date, as a series indexed by date.

    Ranks within each date, then one vectorized Pearson correlation on the ranks.
    Looping ``scipy.stats.spearmanr`` over ~6,600 dates would make early stopping cost
    more than the fit it is watching, and this is also the function `lgbm.ic_score`
    calls on every boosting round.

    Dates whose correlation is undefined — a constant prediction, or a single name —
    are dropped rather than counted as zero, which would silently drag the mean down in
    proportion to how thin the panel is.
    """
    frame = pd.DataFrame({"date": dates, "y": y_true, "p": y_pred}).dropna()
    if frame.empty:
        return pd.Series(dtype="float64", name="ic")

    grouped = frame.groupby("date", observed=True)
    ranks = grouped[["y", "p"]].rank()

    centred = ranks.groupby(frame["date"], observed=True)[["y", "p"]].transform(
        lambda s: s - s.mean()
    )
    numerator = (centred["y"] * centred["p"]).groupby(frame["date"], observed=True).sum()
    denominator = np.sqrt(
        centred["y"].pow(2).groupby(frame["date"], observed=True).sum()
        * centred["p"].pow(2).groupby(frame["date"], observed=True).sum()
    )
    return (numerator / denominator.replace(0.0, np.nan)).dropna().rename("ic")


def icir(ic: pd.Series) -> float:
    """Annualized information ratio of the IC series: mean / std * sqrt(252).

    ICIR is what separates a signal worth trading from one that is right on average and
    unusable in practice — a mean IC of 0.03 that flips sign every other month is not
    the same asset as a mean IC of 0.03 that is positive two thirds of the time.
    """
    if len(ic) < 2:
        return float("nan")
    spread = float(ic.std(ddof=1))
    if spread == 0.0:
        return float("nan")
    return float(ic.mean() / spread * np.sqrt(TRADING_DAYS))


def effective_n(n: int, horizon: int) -> int:
    """Independent observations behind `n` overlapping rows.

    Five-day labels sampled daily overlap four-fifths of the way, so the honest
    denominator for any interval is roughly ``n / horizon``. On the current panel that
    turns 2.9M rows into closer to 580k observations (design doc §5.3).
    """
    return int(n // max(horizon, 1))


def mean_with_ci(values: pd.Series, horizon: int) -> dict[str, float | int]:
    """Mean of a per-period series with a 95% interval widened for overlap.

    The standard error uses `effective_n` rather than `len(values)`. For an IC series
    the periods are dates, and consecutive dates share four days of label, so the naive
    interval is about sqrt(5) too narrow.
    """
    n = len(values)
    n_eff = effective_n(n, horizon)
    mean = float(values.mean()) if n else float("nan")
    if n_eff < 2:
        return {"mean": mean, "ci95_low": float("nan"), "ci95_high": float("nan"),
                "n": n, "effective_n": n_eff}
    half_width = 1.96 * float(values.std(ddof=1)) / np.sqrt(n_eff)
    return {
        "mean": mean,
        "ci95_low": mean - half_width,
        "ci95_high": mean + half_width,
        "n": n,
        "effective_n": n_eff,
    }


def deciles(values: np.ndarray, dates: np.ndarray) -> pd.Series:
    """Per-date deciles 1-10, NULL-safe, matching the GOLD construction.

    Mirrors `mart_features.sql` (the `rank() ... / count(feature) over w_date` block):
    rank against the count of *non-null* values so the ten buckets span the observed
    range, and leave null rows null rather than bucketing them. `ntile` would spread a
    partly-null column across deciles 1-7 and invent an ordering for the nulls.
    """
    frame = pd.DataFrame({"date": dates, "v": values})
    ranks = frame.groupby("date", observed=True)["v"].rank(method="min")
    counts = frame.groupby("date", observed=True)["v"].transform("count")
    bucket = np.floor((ranks - 1) * N_DECILES / counts.replace(0, np.nan)) + 1
    return bucket.clip(upper=N_DECILES).rename("decile")


def long_short_spread(
    y_true: np.ndarray, y_pred: np.ndarray, dates: np.ndarray
) -> pd.Series:
    """Top-decile minus bottom-decile realized return, per date.

    This is the decile spread: what an equal-weight long/short book formed on this
    date's ranking earns over the label horizon. Dates that cannot form both legs are
    dropped.
    """
    frame = pd.DataFrame({"date": dates, "y": y_true, "p": y_pred}).dropna()
    if frame.empty:
        return pd.Series(dtype="float64", name="spread")

    frame["decile"] = deciles(frame["p"].to_numpy(), frame["date"].to_numpy()).to_numpy()
    legs = (
        frame[frame["decile"].isin([1, N_DECILES])]
        .groupby(["date", "decile"], observed=True)["y"]
        .mean()
        .unstack("decile")
    )
    if 1 not in legs.columns or N_DECILES not in legs.columns:
        return pd.Series(dtype="float64", name="spread")
    return (legs[N_DECILES] - legs[1]).dropna().rename("spread")


def tranche_returns(spread: pd.Series, horizon: int) -> list[pd.Series]:
    """Split a per-date spread series into `horizon` non-overlapping tranches.

    A book formed every day on a five-day label is five books, each rebalancing every
    fifth day — the standard overlapping-tranche construction. Slicing the daily spread
    by entry offset recovers those books, and *within* a tranche the returns no longer
    overlap, so a Sharpe computed on one is not inflated by autocorrelation the way one
    computed on the daily series would be.

    Returned in full so #56 can attribute per tranche rather than only in aggregate.
    """
    ordered = spread.sort_index()
    return [ordered.iloc[offset::horizon] for offset in range(horizon)]


def sharpe(returns: pd.Series, horizon: int) -> float:
    """Annualized Sharpe of a series of `horizon`-day returns. Excess is not netted.

    The target is already an excess return (`fwd_ret_5d_excess` is net of the
    cap-weighted market), so there is no risk-free rate to subtract here.
    """
    if len(returns) < 2:
        return float("nan")
    spread = float(returns.std(ddof=1))
    if spread == 0.0:
        return float("nan")
    return float(returns.mean() / spread * np.sqrt(TRADING_DAYS / horizon))


def tranche_sharpe(spread: pd.Series, horizon: int) -> float:
    """Mean annualized Sharpe across the `horizon` overlapping tranches."""
    scores = [sharpe(t, horizon) for t in tranche_returns(spread, horizon)]
    usable = [s for s in scores if np.isfinite(s)]
    return float(np.mean(usable)) if usable else float("nan")


def max_drawdown(returns: pd.Series) -> float:
    """Worst peak-to-trough fall of the compounded equity curve, as a negative number."""
    if returns.empty:
        return float("nan")
    equity = (1.0 + returns.sort_index()).cumprod()
    return float((equity / equity.cummax() - 1.0).min())


def hit_rate(returns: pd.Series) -> float:
    """Fraction of periods the long-short book made money."""
    return float((returns > 0).mean()) if len(returns) else float("nan")


def turnover(
    y_pred: np.ndarray, dates: np.ndarray, symbols: np.ndarray, horizon: int
) -> float:
    """Mean fraction of the long book replaced at each rebalance.

    Overlap is measured between *symbol sets*, and at `horizon` spacing, because that
    is when one tranche actually trades. Compared against #56's cost sweep this is the
    number that decides whether a spread survives transaction costs: a 0.4% spread at
    80% turnover is not a strategy.
    """
    frame = pd.DataFrame({"date": dates, "symbol": symbols, "p": y_pred}).dropna()
    if frame.empty:
        return float("nan")
    frame["decile"] = deciles(frame["p"].to_numpy(), frame["date"].to_numpy()).to_numpy()
    longs = frame[frame["decile"] == N_DECILES]

    rebalances = np.sort(frame["date"].unique())[::horizon]
    books = [
        set(longs.loc[longs["date"] == day, "symbol"]) for day in rebalances
    ]
    changes = [
        1.0 - len(current & previous) / len(current)
        for previous, current in zip(books, books[1:])
        if current
    ]
    return float(np.mean(changes)) if changes else float("nan")


def r2(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Plain coefficient of determination.

    Reported for completeness and expected to be near zero — on a target this noisy an
    R2 above a percent or so is a leak, not a result.
    """
    frame = pd.DataFrame({"y": y_true, "p": y_pred}).dropna()
    if len(frame) < 2:
        return float("nan")
    total = float(((frame["y"] - frame["y"].mean()) ** 2).sum())
    if total == 0.0:
        return float("nan")
    return float(1.0 - ((frame["y"] - frame["p"]) ** 2).sum() / total)


def summarize(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    dates: np.ndarray,
    symbols: np.ndarray,
    horizon: int,
) -> dict:
    """The full metric block for one set of predictions.

    Every caller — the model, each baseline, each breakdown bucket — goes through this,
    so a baseline is never scored more leniently than the model it is a control for.
    """
    ic = ic_by_date(y_true, y_pred, dates)
    spread = long_short_spread(y_true, y_pred, dates)
    tranches = tranche_returns(spread, horizon)
    pooled = pd.concat(tranches) if tranches else pd.Series(dtype="float64")

    return {
        "n_rows": int(len(y_true)),
        "n_dates": int(ic.size),
        "effective_n": effective_n(len(y_true), horizon),
        "ic": mean_with_ci(ic, horizon),
        "icir": icir(ic),
        "ic_positive_rate": hit_rate(ic),
        "decile_spread": mean_with_ci(spread, horizon),
        "long_short_sharpe": tranche_sharpe(spread, horizon),
        "max_drawdown": max_drawdown(pooled),
        "hit_rate": hit_rate(pooled),
        "turnover": turnover(y_pred, dates, symbols, horizon),
        "r2": r2(y_true, y_pred),
    }
