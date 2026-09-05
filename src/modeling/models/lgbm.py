"""LightGBM fitting, early-stopped on rank correlation rather than L2.

L2 on a return target is dominated by the tails — a handful of earnings gaps and
crisis days. A model early-stopped on validation L2 is tuned to predict outliers,
while what actually gets traded is the *ordering*. So the eval metric here is the
mean per-date Spearman information coefficient, and early stopping watches that.

LightGBM is the model for reasons worth recording rather than re-arguing: it routes
NaN natively (34.6% of rows have no fundamentals at all, and imputing would either
invent a meaningful zero or leak cross-sectional information), it handles `sector`
and `industry` as categoricals without encoding, and `shap.TreeExplainer` is exact
and fast on it — the whole selection loop in #55 assumes tree SHAP.
"""

import logging
from dataclasses import dataclass
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from src.modeling.data.splits import Fold

logger = logging.getLogger(__name__)


def ic_score(y_true: np.ndarray, y_pred: np.ndarray, dates: np.ndarray) -> float:
    """Mean per-date Spearman rank correlation.

    Ranks within each date, then takes one vectorized Pearson correlation on the
    ranks. Looping ``scipy.stats.spearmanr`` over ~6,600 dates would make early
    stopping cost more than the fit it is watching.

    Dates whose correlation is undefined — a constant prediction, or a single name —
    are skipped rather than counted as zero, which would silently drag the mean down
    in proportion to how thin the panel is.
    """
    frame = pd.DataFrame({"date": dates, "y": y_true, "p": y_pred}).dropna()
    grouped = frame.groupby("date", observed=True)
    ranks = grouped[["y", "p"]].rank()
    ranks["date"] = frame["date"]

    centred = ranks.groupby("date", observed=True)[["y", "p"]].transform(
        lambda s: s - s.mean()
    )
    products = centred["y"] * centred["p"]
    numerator = products.groupby(frame["date"], observed=True).sum()
    denominator = np.sqrt(
        centred["y"].pow(2).groupby(frame["date"], observed=True).sum()
        * centred["p"].pow(2).groupby(frame["date"], observed=True).sum()
    )

    per_date = (numerator / denominator.replace(0.0, np.nan)).dropna()
    return float(per_date.mean()) if len(per_date) else 0.0


def make_ic_eval(dates: np.ndarray):
    """Return LightGBM's ``(name, value, is_higher_better)`` eval over `dates`."""

    def evaluate(y_pred: np.ndarray, dataset: lgb.Dataset):
        return "ic", ic_score(dataset.get_label(), y_pred, dates), True

    return evaluate


@dataclass
class FoldFit:
    """What one walk-forward fit leaves behind — its score, not its model."""

    fold_index: int
    best_iteration: int
    best_ic: float
    valid_start_fold: int
    valid_end_fold: int
    train_start_date: str
    train_end_date: str
    valid_start_date: str
    valid_end_date: str
    n_train: int
    n_valid: int


def _bounds(dates: pd.Series) -> tuple[str, str]:
    return str(dates.min().date()), str(dates.max().date())


def build_dataset(
    matrix: pd.DataFrame, target: pd.Series, params: dict[str, Any]
) -> lgb.Dataset:
    """Bin the whole panel once, so folds can be views rather than copies.

    This is the difference between fitting on a 16GB laptop and swapping. Slicing the
    DataFrame per fold copies up to 2.4GB each time, on top of the 2.6GB matrix it is
    slicing. Binned once at `max_bin=63`, the panel is roughly one byte per value —
    a few hundred MB — and every fold is a `subset` view over it.

    `free_raw_data=True` lets the float32 originals go as soon as the bins exist. The
    caller can then drop its own references; nothing downstream needs the raw values,
    because prediction at inference reloads from the warehouse.
    """
    dataset = lgb.Dataset(
        matrix,
        label=target,
        free_raw_data=True,
        params={
            "max_bin": params["max_bin"],
            "num_threads": params["num_threads"],
            # Keep every feature: LightGBM would otherwise silently drop columns it
            # judges useless, and the manifest's feature order is a contract.
            "feature_pre_filter": False,
        },
    )
    dataset.construct()
    logger.info("Binned %s rows x %s features", f"{dataset.num_data():,}", dataset.num_feature())
    return dataset


def fit_fold(
    dataset: lgb.Dataset,
    dates: pd.Series,
    fold: Fold,
    params: dict[str, Any],
) -> FoldFit:
    """Fit one fold, early-stopping on validation IC."""
    valid_dates = dates.iloc[fold.valid_idx]

    params = dict(params)
    rounds = params.pop("n_estimators")
    stopping = params.pop("early_stopping_rounds")

    booster = lgb.train(
        params,
        dataset.subset(fold.train_idx),
        num_boost_round=rounds,
        valid_sets=[dataset.subset(fold.valid_idx)],
        valid_names=["valid"],
        feval=make_ic_eval(valid_dates.to_numpy()),
        callbacks=[
            lgb.early_stopping(stopping, first_metric_only=False, verbose=False),
            lgb.log_evaluation(period=100),
        ],
    )

    best_ic = booster.best_score["valid"]["ic"]
    best_iteration = booster.best_iteration
    train_bounds = _bounds(dates.iloc[fold.train_idx])
    valid_bounds = _bounds(valid_dates)
    logger.info(
        "fold %s (fold_id %s-%s): IC %.4f at iteration %s",
        fold.index,
        fold.valid_start_fold,
        fold.valid_end_fold,
        best_ic,
        best_iteration,
    )
    # The booster is deliberately not retained. All that is needed downstream is the
    # iteration count and the score; holding fifteen fitted models alive costs memory
    # for nothing, and the shipped model is a separate refit.
    del booster
    return FoldFit(
        fold_index=fold.index,
        best_iteration=best_iteration,
        best_ic=float(best_ic),
        valid_start_fold=fold.valid_start_fold,
        valid_end_fold=fold.valid_end_fold,
        train_start_date=train_bounds[0],
        train_end_date=train_bounds[1],
        valid_start_date=valid_bounds[0],
        valid_end_date=valid_bounds[1],
        n_train=len(fold.train_idx),
        n_valid=len(fold.valid_idx),
    )


def fit_final(
    dataset: lgb.Dataset,
    keep: np.ndarray,
    params: dict[str, Any],
    n_estimators: int,
) -> lgb.Booster:
    """Fit the shipped model on every pre-holdout row.

    No validation set and no early stopping — the fold fits already decided the
    iteration count. Unlike the last walk-forward model, whose training set stops
    before its own validation window, this one has seen the whole pre-holdout panel.
    """
    params = dict(params)
    params.pop("n_estimators", None)
    params.pop("early_stopping_rounds", None)
    logger.info("Final fit on %s rows for %s rounds", f"{len(keep):,}", n_estimators)
    return lgb.train(
        params, dataset.subset(keep), num_boost_round=n_estimators
    )
