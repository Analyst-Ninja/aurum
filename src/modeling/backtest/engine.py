"""Run the book, sweep the costs, and try to disprove the result.

The engine is deliberately two numbers wide. Everything downstream — the cost sweep,
the 500-run randomization null, the signal-lag check — is a function of a per-formation
`spread` series and a per-formation `turnover` series, both computed once. That is what
makes 500 reruns seconds rather than hours, and it is why `run()` allocates nothing
beyond one arithmetic pass.

Sharpe is always taken across the five non-overlapping tranches, never on the daily
series. Consecutive daily spreads share four of their five label days, so a Sharpe on
the daily series is inflated by pure autocorrelation.
"""

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats

from src.modeling.backtest import costs, portfolio
from src.modeling.evaluate import metrics

logger = logging.getLogger(__name__)

TRADING_DAYS = 252


@dataclass
class Book:
    """The precomputed inputs every simulation below reuses."""

    spread: pd.Series
    turnover: pd.Series
    horizon: int


def build_book(predictions: pd.DataFrame, horizon: int) -> Book:
    """Rank, form the decile legs, and reduce them to spread and turnover."""
    spread = metrics.long_short_spread(
        predictions["y"].to_numpy(dtype="float64"),
        predictions["pred"].to_numpy(dtype="float64"),
        predictions["date"].to_numpy(),
    )
    turnover = portfolio.rebalance_turnover(predictions, horizon)
    return Book(spread=spread, turnover=turnover, horizon=horizon)


def run(book: Book, cost_bps: float) -> pd.Series:
    """Net per-formation returns at `cost_bps` per side."""
    return costs.net_returns(book.spread, book.turnover, cost_bps)


def tranche_sharpe(net: pd.Series, horizon: int) -> float:
    """Mean annualized Sharpe across the `horizon` non-overlapping tranches."""
    scores = [
        metrics.sharpe(series, horizon) for series in metrics.tranche_returns(net, horizon)
    ]
    usable = [score for score in scores if np.isfinite(score)]
    return float(np.mean(usable)) if usable else float("nan")


def daily_returns(net: pd.Series, horizon: int) -> pd.Series:
    """Portfolio return per calendar day: one vintage's `horizon`-day return over five.

    1/`horizon` of capital is committed on each formation date, so the book's daily
    contribution is that vintage's return spread over the days it is held. Used for the
    equity curve and the yearly table; never for a Sharpe.
    """
    return (net.sort_index() / horizon).rename("daily")


def equity_curve(net: pd.Series, horizon: int) -> pd.Series:
    """Compounded equity of the daily series, starting at 1.0."""
    return (1.0 + daily_returns(net, horizon)).cumprod().rename("equity")


def sweep(book: Book, grid: list[float]) -> dict:
    """Net Sharpe at each cost level, plus the break-even."""
    levels = {
        f"{int(cost)}bps": {
            "sharpe": tranche_sharpe(run(book, cost), book.horizon),
            "mean_return": float(run(book, cost).mean()),
        }
        for cost in grid
    }
    return {
        "by_cost_bps": levels,
        "break_even_bps": costs.break_even_bps(book.spread, book.turnover),
    }


def _shuffled_spreads(
    predictions: pd.DataFrame, book: Book, n_shuffles: int, seed: int
) -> np.ndarray:
    """Sharpes of `n_shuffles` books whose ranking carries no information.

    The null shuffles realized returns *within each date*, which leaves the position
    structure, the calendar, the universe and the turnover exactly as they are and
    destroys only the pairing between prediction and outcome. Shuffling the predictions
    instead would also randomize the books and confound two changes at once.
    """
    frame = predictions.dropna(subset=["pred", "y"]).copy()
    frame["decile"] = metrics.deciles(
        frame["pred"].to_numpy(), frame["date"].to_numpy()
    ).to_numpy()
    frame = frame[frame["decile"].isin([1, metrics.N_DECILES])].sort_values("date")

    y = frame["y"].to_numpy(dtype="float64")
    is_long = (frame["decile"] == metrics.N_DECILES).to_numpy()
    codes, dates = pd.factorize(frame["date"], sort=True)
    starts = np.searchsorted(codes, np.arange(len(dates)))
    ends = np.append(starts[1:], len(codes))

    rng = np.random.default_rng(seed)
    sharpes = np.empty(n_shuffles)
    for shuffle in range(n_shuffles):
        drawn = y.copy()
        for start, end in zip(starts, ends):
            rng.shuffle(drawn[start:end])
        spread = pd.Series(
            [
                drawn[start:end][is_long[start:end]].mean()
                - drawn[start:end][~is_long[start:end]].mean()
                for start, end in zip(starts, ends)
            ],
            index=dates,
        ).dropna()
        null = Book(spread=spread, turnover=book.turnover, horizon=book.horizon)
        sharpes[shuffle] = tranche_sharpe(run(null, 0.0), book.horizon)
    return sharpes


def randomization_test(
    predictions: pd.DataFrame, book: Book, n_shuffles: int, seed: int
) -> dict:
    """Where the real Sharpe sits against a no-information null. The missing p-value."""
    actual = tranche_sharpe(run(book, 0.0), book.horizon)
    null = _shuffled_spreads(predictions, book, n_shuffles, seed)
    finite = null[np.isfinite(null)]
    percentile = float((finite < actual).mean() * 100.0) if len(finite) else float("nan")
    return {
        "n_shuffles": int(n_shuffles),
        "actual_sharpe": actual,
        "percentile": percentile,
        "p_value": (1.0 - percentile / 100.0) if np.isfinite(percentile) else float("nan"),
        "null_mean": float(finite.mean()) if len(finite) else float("nan"),
        "null_std": float(finite.std(ddof=1)) if len(finite) > 1 else float("nan"),
        "null_sharpes": finite.tolist(),
    }


def signal_lag_test(predictions: pd.DataFrame, horizon: int) -> dict:
    """Rerun with the signal delayed one trading day.

    A real 5-day signal decays gracefully. A collapse means the model is using
    information from the bar it trades on, and the leak is upstream in the warehouse,
    not here.
    """
    calendar = np.sort(predictions["date"].unique())
    following = dict(zip(calendar[:-1], calendar[1:]))
    lagged = predictions.assign(date_lagged=predictions["date"].map(following)).dropna(
        subset=["date_lagged"]
    )
    # The prediction moves forward a day; the outcome stays with its own date, so the
    # book formed on day d+1 is the ranking from day d scored on day d+1's returns.
    shifted = lagged[["date_lagged", "symbol", "pred"]].rename(
        columns={"date_lagged": "date"}
    )
    joined = predictions[["date", "symbol", "y"]].merge(
        shifted, on=["date", "symbol"], how="inner"
    )

    book = build_book(joined, horizon)
    return {
        "sharpe": tranche_sharpe(run(book, 0.0), horizon),
        "mean_spread": float(book.spread.mean()) if len(book.spread) else float("nan"),
        "n_dates": int(len(book.spread)),
    }


def deflated_sharpe(observed: float, n_trials: int, n_obs: int) -> float:
    """Bailey & Lopez de Prado's deflated Sharpe, in probability terms.

    Selecting the best of many configurations manufactures Sharpe out of noise: the
    expected maximum Sharpe of `n_trials` worthless strategies is well above zero. This
    returns the probability the observed Sharpe beats that expected maximum.
    """
    if not np.isfinite(observed) or n_obs < 2:
        return float("nan")
    trials = max(int(n_trials), 1)
    if trials == 1:
        expected_max = 0.0
    else:
        euler = 0.5772156649
        expected_max = (1 - euler) * stats.norm.ppf(
            1 - 1.0 / trials
        ) + euler * stats.norm.ppf(1 - 1.0 / (trials * np.e))
    return float(stats.norm.cdf((observed - expected_max) * np.sqrt(n_obs - 1)))


def yearly_table(net: pd.Series, horizon: int) -> pd.DataFrame:
    """Net return, Sharpe and max drawdown per calendar year.

    The most honest artifact in the report: it shows at a glance whether the result is
    one good decade carrying fifteen flat years.
    """
    daily = daily_returns(net, horizon)
    if daily.empty:
        return pd.DataFrame(columns=["year", "net_return", "sharpe", "max_drawdown"])

    years = pd.DatetimeIndex(daily.index).year
    rows = [
        {
            "year": int(year),
            "net_return": float((1.0 + daily[years == year]).prod() - 1.0),
            "sharpe": tranche_sharpe(net[pd.DatetimeIndex(net.index).year == year], horizon),
            "max_drawdown": metrics.max_drawdown(daily[years == year]),
        }
        for year in sorted(set(years))
    ]
    return pd.DataFrame(rows)
