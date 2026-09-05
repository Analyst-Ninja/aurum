"""Does the engine measure what it claims — on signals whose answer is known."""

import numpy as np
import pandas as pd

from src.modeling.backtest import costs, engine, portfolio

HORIZON = 5
N_DATES = 200
N_SYMBOLS = 100


def _panel(seed: int, informative: bool) -> pd.DataFrame:
    """A synthetic prediction panel. `informative` makes `pred` rank `y` perfectly."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-01", periods=N_DATES)
    symbols = [f"S{i:03d}" for i in range(N_SYMBOLS)]

    frame = pd.DataFrame(
        {
            "date": np.repeat(dates, N_SYMBOLS),
            "symbol": symbols * N_DATES,
            "y": rng.normal(0.0, 0.03, N_DATES * N_SYMBOLS),
        }
    )
    frame["pred"] = (
        frame["y"] if informative else rng.normal(size=len(frame))
    )
    return frame


def test_a_perfect_signal_earns_a_large_sharpe():
    book = engine.build_book(_panel(0, informative=True), HORIZON)
    assert engine.tranche_sharpe(engine.run(book, 0.0), HORIZON) > 5.0
    assert book.spread.mean() > 0.0


def test_a_random_signal_sits_inside_its_own_null_band():
    predictions = _panel(1, informative=False)
    book = engine.build_book(predictions, HORIZON)

    result = engine.randomization_test(predictions, book, n_shuffles=100, seed=3)
    assert 2.0 < result["percentile"] < 98.0
    assert abs(result["actual_sharpe"]) < 3.0


def test_the_randomization_null_ranks_a_perfect_signal_at_the_top():
    predictions = _panel(2, informative=True)
    book = engine.build_book(predictions, HORIZON)

    result = engine.randomization_test(predictions, book, n_shuffles=100, seed=4)
    assert result["percentile"] == 100.0
    assert result["p_value"] == 0.0


def test_five_tranches_trade_two_fifths_of_the_book_a_day_not_twice_over():
    predictions = _panel(5, informative=False)
    turnover = portfolio.rebalance_turnover(predictions, HORIZON)

    # A random signal churns a vintage almost completely at its own rebalance, which is
    # the worst case. Even then the book only trades 2/horizon of itself per day,
    # because only one of the five vintages rebalances on any given day.
    assert turnover.mean() > 0.8
    daily = 2.0 * turnover.mean() / HORIZON
    assert 0.3 < daily < 0.45


def test_positions_are_dollar_neutral_and_size_one_vintage():
    positions = portfolio.build_positions(_panel(6, informative=False), HORIZON)
    by_date = positions.groupby("date")["weight"]

    assert np.allclose(by_date.sum().to_numpy(), 0.0, atol=1e-12)
    assert np.allclose(by_date.apply(lambda w: w.abs().sum()).to_numpy(), 1.0 / HORIZON)
    assert positions["tranche"].nunique() == HORIZON


def test_returns_fall_monotonically_as_costs_rise():
    book = engine.build_book(_panel(7, informative=True), HORIZON)
    means = [float(engine.run(book, cost).mean()) for cost in (0.0, 5.0, 10.0, 20.0)]
    assert means == sorted(means, reverse=True)


def test_break_even_is_the_cost_that_zeroes_the_mean_return():
    book = engine.build_book(_panel(8, informative=True), HORIZON)
    # A synthetic perfect signal earns ~11% per vintage, so its break-even is hundreds
    # of bps; the production ceiling would clip it and hide the crossing.
    level = costs.break_even_bps(book.spread, book.turnover, ceiling=10_000.0)

    assert level > 0.0
    assert abs(float(engine.run(book, level).mean())) < 1e-9
    assert float(engine.run(book, level * 1.5).mean()) < 0.0


def test_break_even_is_zero_when_the_gross_spread_already_loses():
    spread = pd.Series([-0.01, -0.02], index=pd.to_datetime(["2020-01-01", "2020-01-02"]))
    assert costs.break_even_bps(spread, pd.Series(dtype="float64")) == 0.0


def test_signal_lag_collapses_a_same_bar_signal():
    predictions = _panel(9, informative=True)
    gross = engine.tranche_sharpe(
        engine.run(engine.build_book(predictions, HORIZON), 0.0), HORIZON
    )
    lagged = engine.signal_lag_test(predictions, HORIZON)

    # `y` is pure noise across dates, so a signal that only knows its own bar has
    # nothing left once it is delayed.
    assert lagged["sharpe"] < gross / 4.0


def test_deflated_sharpe_falls_as_more_configurations_are_tried():
    one = engine.deflated_sharpe(1.0, n_trials=1, n_obs=200)
    many = engine.deflated_sharpe(1.0, n_trials=500, n_obs=200)
    assert one > many
    assert 0.0 <= many <= 1.0


def test_yearly_table_covers_every_calendar_year_in_the_series():
    book = engine.build_book(_panel(10, informative=True), HORIZON)
    yearly = engine.yearly_table(engine.run(book, 10.0), HORIZON)

    assert list(yearly.columns) == ["year", "net_return", "sharpe", "max_drawdown"]
    assert yearly["year"].tolist() == [2020]
