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
from collections import deque
from dataclasses import dataclass
from itertools import count
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from src.modeling.data.splits import Fold
from src.modeling.evaluate.metrics import ic_by_date

logger = logging.getLogger(__name__)


def ic_score(y_true: np.ndarray, y_pred: np.ndarray, dates: np.ndarray) -> float:
    """Mean per-date Spearman rank correlation.

    The per-date series lives in `evaluate.metrics.ic_by_date`, because #54 needs the
    series itself — ICIR is its dispersion, not its mean — and two implementations of
    the same correlation would eventually disagree about which dates are usable. This
    is the early-stopping metric; it wants one number.
    """
    per_date = ic_by_date(y_true, y_pred, dates)
    return float(per_date.mean()) if len(per_date) else 0.0


def make_ic_eval(dates: np.ndarray, history: deque | None = None):
    """Return LightGBM's ``(name, value, is_higher_better)`` eval over `dates`.

    When `history` is given, each round's validation predictions are pushed onto it.
    That is the only way to recover out-of-sample fold predictions here: the panel is
    binned with ``free_raw_data=True`` and the raw matrix is dropped before the folds
    run, so there is nothing left to call ``booster.predict`` on afterwards. A bounded
    deque keeps only the rounds early stopping can still choose between.
    """

    rounds = count(1)

    def evaluate(y_pred: np.ndarray, dataset: lgb.Dataset):
        if history is not None:
            history.append((next(rounds), y_pred.astype("float32")))
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
    # Out-of-sample predictions on this fold's validation rows, in `fold.valid_idx`
    # order. #54 needs them: a fold's mean IC is one number, and decile spread, Sharpe
    # and the baselines cannot be recovered from it. Kept as float32 — 2.4M values
    # across fifteen folds is ~10MB, against the ~250MB a retained booster would cost.
    valid_pred: np.ndarray


def _bounds(dates: pd.Series) -> tuple[str, str]:
    return str(dates.min().date()), str(dates.max().date())


def _predictions_at(
    history: deque[tuple[int, np.ndarray]], iteration: int
) -> np.ndarray:
    """The recorded validation predictions for `iteration`, or the last round's.

    The fallback covers the case where early stopping never fired and the winning round
    has already been evicted — the model then ran to its cap, and the last round is the
    one that would be shipped anyway.
    """
    for round_index, predictions in history:
        if round_index == iteration:
            return predictions
    return history[-1][1] if history else np.empty(0, dtype="float32")


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

    # Early stopping cannot pick a round more than `stopping` behind the last one, so a
    # deque this long always still holds the winner. Bounded on purpose: keeping every
    # round would be 1,500 copies of the validation predictions.
    history: deque[tuple[int, np.ndarray]] = deque(maxlen=stopping + 1)

    booster = lgb.train(
        params,
        dataset.subset(fold.train_idx),
        num_boost_round=rounds,
        valid_sets=[dataset.subset(fold.valid_idx)],
        valid_names=["valid"],
        feval=make_ic_eval(valid_dates.to_numpy(), history),
        callbacks=[
            lgb.early_stopping(stopping, first_metric_only=False, verbose=False),
            lgb.log_evaluation(period=100),
        ],
    )

    best_ic = booster.best_score["valid"]["ic"]
    best_iteration = booster.best_iteration
    valid_pred = _predictions_at(history, best_iteration)
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
        valid_pred=valid_pred,
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
