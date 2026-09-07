"""Degenerate inputs to the metric block.

Every one of these returns NaN rather than a number, and the distinction matters: a
zero IC and an undefined IC read the same in a report but mean opposite things.
"""

import numpy as np
import pandas as pd

from src.modeling.evaluate import metrics


def test_an_all_null_panel_has_no_ic_rather_than_an_ic_of_zero():
    empty = metrics.ic_by_date(
        np.array([np.nan, np.nan]), np.array([np.nan, np.nan]),
        np.array(["2024-01-01", "2024-01-02"]),
    )

    assert empty.empty


def test_an_all_null_panel_forms_no_decile_spread():
    spread = metrics.long_short_spread(
        np.array([np.nan]), np.array([np.nan]), np.array(["2024-01-01"])
    )

    assert spread.empty


def test_turnover_of_an_all_null_panel_is_undefined():
    assert np.isnan(
        metrics.turnover(
            np.array([np.nan]), np.array(["2024-01-01"]), np.array(["AAPL"]), 5
        )
    )


def test_a_single_ic_observation_has_no_dispersion_to_scale_by():
    assert np.isnan(metrics.icir(pd.Series([0.02])))


def test_a_constant_ic_series_has_an_undefined_icir():
    """Zero dispersion is a division by zero, not an infinite information ratio."""
    assert np.isnan(metrics.icir(pd.Series([0.02, 0.02, 0.02])))


def test_a_single_return_has_no_sharpe():
    assert np.isnan(metrics.sharpe(pd.Series([0.01]), 5))


def test_a_constant_return_series_has_an_undefined_sharpe():
    assert np.isnan(metrics.sharpe(pd.Series([0.01, 0.01, 0.01]), 5))


def test_r2_needs_two_points():
    assert np.isnan(metrics.r2(np.array([0.1]), np.array([0.1])))


def test_r2_against_a_constant_truth_is_undefined():
    """Total variance of zero leaves nothing for the model to explain."""
    assert np.isnan(metrics.r2(np.array([0.5, 0.5, 0.5]), np.array([0.0, 0.2, 0.1])))


def test_an_empty_return_series_has_no_drawdown():
    assert np.isnan(metrics.max_drawdown(pd.Series(dtype="float64")))


def test_a_spread_that_never_forms_both_legs_is_empty():
    """One symbol a date cannot fill decile 1 and decile 10 at once."""
    spread = metrics.long_short_spread(
        np.array([0.01, 0.02]), np.array([1.0, 2.0]),
        np.array(["2024-01-01", "2024-01-02"]),
    )

    assert spread.empty
