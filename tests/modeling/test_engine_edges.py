"""The edges of the backtest engine and the cost model.

Each of these is a branch that only fires on a degenerate input — an empty series, a
single trial, a book with no turnover — and each one silently returns a plausible
number if it is wrong.
"""

import numpy as np
import pandas as pd
import pytest

from src.modeling.backtest import costs, engine, portfolio


def test_a_single_trial_is_not_deflated_at_all():
    """With one configuration tried there is no selection to correct for."""
    single = engine.deflated_sharpe(1.0, 1, 100)
    many = engine.deflated_sharpe(1.0, 500, 100)

    assert single > many


def test_deflation_needs_at_least_two_observations():
    assert np.isnan(engine.deflated_sharpe(1.0, 10, 1))


def test_deflation_of_an_undefined_sharpe_is_undefined():
    assert np.isnan(engine.deflated_sharpe(float("nan"), 10, 100))


def test_an_empty_book_yields_an_empty_yearly_table():
    empty = engine.yearly_table(pd.Series(dtype="float64"), 5)

    assert empty.empty
    assert list(empty.columns) == ["year", "net_return", "sharpe", "max_drawdown"]


def test_turnover_of_a_book_with_no_members_is_an_empty_series():
    predictions = pd.DataFrame(columns=["date", "symbol", "y", "pred"])

    assert portfolio.rebalance_turnover(predictions, 5).empty


def test_the_cost_model_is_a_half_spread_plus_square_root_impact():
    per_side = costs.cost_rate_bps(vol_21d=0.02, participation=0.04, half_spread_bps=5.0,
                              impact_k=0.1)

    assert per_side == pytest.approx(5.0 + 0.1 * 0.02 * 0.2)


def test_a_book_that_never_trades_breaks_even_only_at_the_ceiling():
    spread = pd.Series([0.001, 0.002], index=pd.to_datetime(["2024-01-01", "2024-01-08"]))
    no_turnover = pd.Series(0.0, index=spread.index)

    assert costs.break_even_bps(spread, no_turnover, ceiling=200.0) == 200.0
