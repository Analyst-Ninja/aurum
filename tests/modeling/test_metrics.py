import numpy as np
import pandas as pd
import pytest
from scipy.stats import spearmanr

from src.modeling.evaluate import metrics


def _panel(n_dates=40, n_symbols=50, seed=0):
    rng = np.random.default_rng(seed)
    dates = np.repeat(pd.date_range("2026-01-01", periods=n_dates, freq="B"), n_symbols)
    symbols = np.tile([f"S{i:03d}" for i in range(n_symbols)], n_dates)
    y = rng.normal(size=len(dates))
    return dates, symbols, y


def test_ic_by_date_matches_scipy_per_date():
    """The acceptance criterion: the vectorized IC equals a per-date scipy Spearman."""
    dates, _, y = _panel()
    p = np.random.default_rng(1).normal(size=len(y))
    frame = pd.DataFrame({"date": dates, "y": y, "p": p})

    reference = pd.Series(
        {
            day: spearmanr(group["y"], group["p"]).statistic
            for day, group in frame.groupby("date")
        }
    )
    result = metrics.ic_by_date(y, p, dates)

    assert len(result) == len(reference)
    np.testing.assert_allclose(result.to_numpy(), reference.to_numpy(), atol=1e-12)


def test_ic_is_one_for_perfect_prediction_and_zero_for_shuffled():
    """The second acceptance criterion, both halves."""
    dates, _, y = _panel(n_dates=200, n_symbols=100)

    perfect = metrics.ic_by_date(y, y, dates)
    assert perfect.mean() == pytest.approx(1.0)

    rng = np.random.default_rng(7)
    shuffled = rng.permutation(y)
    assert metrics.ic_by_date(y, shuffled, dates).mean() == pytest.approx(0.0, abs=0.02)


def test_ic_by_date_drops_dates_with_no_ordering():
    dates = np.repeat(pd.date_range("2026-01-01", periods=2), 4)
    y = np.array([1.0, 2, 3, 4, 1, 2, 3, 4])
    p = np.array([1.0, 2, 3, 4, 9, 9, 9, 9])  # second date is flat

    result = metrics.ic_by_date(y, p, dates)
    assert len(result) == 1
    assert result.iloc[0] == pytest.approx(1.0)


def test_icir_scales_the_mean_by_its_dispersion():
    ic = pd.Series([0.02, 0.04, 0.02, 0.04])
    expected = ic.mean() / ic.std(ddof=1) * np.sqrt(metrics.TRADING_DAYS)

    assert metrics.icir(ic) == pytest.approx(expected)
    assert np.isnan(metrics.icir(pd.Series([0.03])))


def test_deciles_span_ten_buckets_and_leave_nulls_null():
    dates = np.repeat(pd.Timestamp("2026-01-01"), 100)
    values = np.arange(100.0)
    values[:20] = np.nan

    result = metrics.deciles(values, dates)

    assert result[:20].isna().all()
    assert set(result.dropna().unique()) == set(range(1, 11))
    # 80 non-null values over ten buckets — the nulls must not squeeze the range.
    assert result.dropna().value_counts().unique().tolist() == [8]


def test_long_short_spread_is_top_minus_bottom_decile():
    dates = np.repeat(pd.date_range("2026-01-01", periods=3), 100)
    pred = np.tile(np.arange(100.0), 3)
    y = pred / 100.0  # perfectly aligned, so the spread is the decile gap in y

    spread = metrics.long_short_spread(y, pred, dates)

    assert len(spread) == 3
    assert spread.iloc[0] == pytest.approx(0.9)


def test_tranche_returns_splits_into_horizon_non_overlapping_series():
    spread = pd.Series(np.arange(20.0), index=pd.date_range("2026-01-01", periods=20))

    tranches = metrics.tranche_returns(spread, horizon=5)

    assert len(tranches) == 5
    assert all(len(t) == 4 for t in tranches)
    # Every observation lands in exactly one tranche.
    assert sum(len(t) for t in tranches) == len(spread)


def test_effective_n_divides_by_the_label_horizon():
    assert metrics.effective_n(2_900_000, 5) == 580_000
    assert metrics.effective_n(3, 5) == 0


def test_mean_with_ci_widens_the_interval_for_overlap():
    values = pd.Series(np.random.default_rng(0).normal(size=500))

    naive = metrics.mean_with_ci(values, horizon=1)
    overlapping = metrics.mean_with_ci(values, horizon=5)

    assert overlapping["effective_n"] == 100
    width = overlapping["ci95_high"] - overlapping["ci95_low"]
    assert width == pytest.approx(
        (naive["ci95_high"] - naive["ci95_low"]) * np.sqrt(5), rel=1e-9
    )


def test_turnover_is_zero_for_a_static_book_and_one_for_a_disjoint_one():
    dates = np.repeat(pd.date_range("2026-01-01", periods=10), 50)
    symbols = np.tile([f"S{i:02d}" for i in range(50)], 10)

    static = np.tile(np.arange(50.0), 10)
    assert metrics.turnover(static, dates, symbols, horizon=5) == pytest.approx(0.0)

    flipped = np.tile(np.arange(50.0), 10)
    flipped[250:] = np.tile(np.arange(50.0)[::-1], 5)
    assert metrics.turnover(flipped, dates, symbols, horizon=5) == pytest.approx(1.0)


def test_summarize_reports_every_headline_metric():
    dates, symbols, y = _panel(n_dates=120, n_symbols=60)
    pred = y + np.random.default_rng(3).normal(scale=2.0, size=len(y))

    block = metrics.summarize(y, pred, dates, symbols, horizon=5)

    assert block["ic"]["mean"] > 0
    assert block["ic"]["effective_n"] == metrics.effective_n(120, 5)
    assert block["decile_spread"]["mean"] > 0
    assert set(block) == {
        "n_rows",
        "n_dates",
        "effective_n",
        "ic",
        "icir",
        "ic_positive_rate",
        "decile_spread",
        "long_short_sharpe",
        "max_drawdown",
        "hit_rate",
        "turnover",
        "r2",
    }
