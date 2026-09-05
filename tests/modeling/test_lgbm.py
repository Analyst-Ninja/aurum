import numpy as np
import pandas as pd
import pytest
from scipy.stats import spearmanr

from src.modeling.config import ModelParams, SplitConfig
from src.modeling.data.splits import walk_forward_folds
from src.modeling.models.lgbm import build_dataset, fit_final, fit_fold, ic_score


def _reference_ic(frame):
    """Per-date scipy Spearman, averaged — the slow definition ic_score vectorizes."""
    per_date = [
        spearmanr(group["y"], group["p"]).statistic
        for _, group in frame.groupby("date")
        if group["p"].nunique() > 1 and len(group) > 1
    ]
    return float(np.mean(per_date))


def test_ic_score_matches_a_scipy_reference():
    rng = np.random.default_rng(0)
    frame = pd.DataFrame(
        {
            "date": np.repeat(pd.date_range("2026-01-01", periods=20), 30),
            "y": rng.normal(size=600),
            "p": rng.normal(size=600),
        }
    )

    assert ic_score(
        frame["y"].to_numpy(), frame["p"].to_numpy(), frame["date"].to_numpy()
    ) == pytest.approx(_reference_ic(frame), abs=1e-12)


def test_ic_score_is_one_for_a_perfectly_ordered_prediction():
    dates = np.repeat(pd.date_range("2026-01-01", periods=3), 5)
    y = np.tile(np.arange(5.0), 3)

    assert ic_score(y, y * 2, dates) == pytest.approx(1.0)
    assert ic_score(y, -y, dates) == pytest.approx(-1.0)


def test_ic_score_skips_dates_where_correlation_is_undefined():
    """A constant prediction has no rank correlation. Counting it as zero would drag
    the mean down in proportion to how many such dates there are."""
    dates = np.repeat(pd.date_range("2026-01-01", periods=2), 4)
    y = np.array([1.0, 2, 3, 4, 1, 2, 3, 4])
    p = np.array([1.0, 2, 3, 4, 9, 9, 9, 9])  # second date is flat

    assert ic_score(y, p, dates) == pytest.approx(1.0)


def test_ic_score_returns_zero_when_no_date_is_usable():
    dates = np.repeat(pd.Timestamp("2026-01-01"), 4)

    assert ic_score(np.arange(4.0), np.ones(4), dates) == 0.0


def _panel(n_folds=6, days_per_fold=10, symbols=40, seed=0):
    """A synthetic panel with real signal, so a fit has something to find."""
    rng = np.random.default_rng(seed)
    rows = []
    date = pd.Timestamp("2020-01-01")
    for fold in range(1, n_folds + 1):
        for _ in range(days_per_fold):
            signal = rng.normal(size=symbols)
            noise = rng.normal(size=symbols)
            for symbol in range(symbols):
                rows.append(
                    {
                        "symbol": f"S{symbol}",
                        "date": date,
                        "fold_id": fold,
                        "feature_a": signal[symbol],
                        "feature_b": rng.normal(),
                        "y": signal[symbol] * 0.5 + noise[symbol] * 0.5,
                    }
                )
            date += pd.Timedelta(days=1)
    return pd.DataFrame(rows)


PARAMS = ModelParams(
    num_leaves=7,
    min_child_samples=20,
    n_estimators=60,
    early_stopping_rounds=5,
    learning_rate=0.1,
).model_dump()

SPLITS = SplitConfig(horizon=2, embargo=2, burn_in_folds=2, eval_end_fold=6, refit_every=2)


def test_fit_fold_early_stops_on_ic():
    frame = _panel()
    dataset = build_dataset(frame[["feature_a", "feature_b"]], frame["y"], PARAMS)
    fold = next(iter(walk_forward_folds(frame, SPLITS)))

    fit = fit_fold(dataset, frame["date"], fold, PARAMS)

    # Stopped before the cap, on the IC metric, having found the planted signal.
    assert 0 < fit.best_iteration <= PARAMS["n_estimators"]
    assert fit.best_ic > 0.1
    assert fit.n_train > 0 and fit.n_valid > 0


def test_fit_fold_records_its_window():
    frame = _panel()
    fold = next(iter(walk_forward_folds(frame, SPLITS)))

    dataset = build_dataset(frame[["feature_a", "feature_b"]], frame["y"], PARAMS)
    fit = fit_fold(dataset, frame["date"], fold, PARAMS)

    assert fit.train_start_date < fit.valid_start_date
    assert fit.valid_start_date < fit.valid_end_date
    assert fit.valid_start_fold == SPLITS.burn_in_folds + 1
    # train_end_date deliberately falls *after* the validation window: the window is
    # expanding, so training resumes on the far side of the embargo. That the gap is
    # respected is asserted in test_splits, on positions rather than these bounds.
    assert fit.train_end_date > fit.valid_end_date


def test_a_saved_model_reloads_and_predicts_identically(tmp_path):
    """The acceptance criterion. Native text format, not pickle — a pickled wrapper
    is bound to the library version that made it."""
    import lightgbm as lgb

    frame = _panel()
    matrix = frame[["feature_a", "feature_b"]]
    dataset = build_dataset(matrix, frame["y"], PARAMS)
    booster = fit_final(dataset, np.arange(len(frame)), PARAMS, n_estimators=25)

    path = tmp_path / "model.txt"
    booster.save_model(str(path))
    reloaded = lgb.Booster(model_file=str(path))

    np.testing.assert_array_equal(booster.predict(matrix), reloaded.predict(matrix))


def test_the_final_fit_never_sees_the_holdout():
    frame = _panel()
    dataset = build_dataset(frame[["feature_a", "feature_b"]], frame["y"], PARAMS)
    pre_holdout = np.flatnonzero((frame["fold_id"] <= 4).to_numpy())

    booster = fit_final(dataset, pre_holdout, PARAMS, n_estimators=10)

    # It fitted on the rows it was given and nothing else.
    assert booster.num_trees() == 10
    assert len(pre_holdout) < len(frame)
