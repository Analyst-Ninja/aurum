"""Purged, embargoed, expanding walk-forward splits.

``fwd_ret_5d`` on daily bars means consecutive rows share four of five days of label,
so a training row three days before a validation window carries a label reaching
*into* it. The naive split the warehouse suggests — train ``fold_id <= k``, validate
``fold_id = k+1`` — therefore leaks, and every out-of-sample number it produces is
optimistic. Purge and embargo are the standard fix (Lopez de Prado, *Advances in
Financial Machine Learning*, ch. 7).

Both operate on **dates, not fold ids**. ``fold_id`` is a dense rank over calendar
months; purging by fold would drop a whole month to remove five days.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.modeling.config import SplitConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fold:
    """One walk-forward fit: an expanding training set and the year it predicts."""

    index: int
    train_idx: np.ndarray
    valid_idx: np.ndarray
    valid_start_fold: int
    valid_end_fold: int

    def __repr__(self) -> str:
        return (
            f"Fold(index={self.index}, train={len(self.train_idx):,}, "
            f"valid={len(self.valid_idx):,}, "
            f"folds {self.valid_start_fold}-{self.valid_end_fold})"
        )


def sample_weights(dates: pd.Series, config: SplitConfig) -> np.ndarray | None:
    """Exponential decay weights by age, or None for uniform.

    Off by default. More history helps a low signal-to-noise problem, but a 2003
    regime is less relevant than a 2025 one; this manages that trade-off without
    discarding history outright.
    """
    if config.decay_half_life_years is None:
        return None
    age_years = (dates.max() - dates).dt.days / 365.25
    return np.asarray(0.5 ** (age_years / config.decay_half_life_years), dtype="float64")


def walk_forward_folds(
    frame: pd.DataFrame, config: SplitConfig, unlock_holdout: bool = False
) -> Iterator[Fold]:
    """Yield expanding folds whose training rows cannot see their validation window.

    The schedule over the panel's 321 monthly folds:

    ===========  ===========================================================
    1-120        burn-in — training only, never evaluated
    121-297      evaluation — refit every ``refit_every`` folds
    298-321      final holdout — never yielded unless ``unlock_holdout``
    ===========  ===========================================================

    The holdout is enforced rather than honoured. Touching it more than once, after
    the feature set and hyperparameters are frozen, is how a holdout stops being one.
    """
    fold_ids = frame["fold_id"].to_numpy()
    # Position of each row's date within the panel's trading calendar, so that the
    # horizon and embargo are counted in trading days rather than calendar days.
    calendar = np.sort(frame["date"].unique())
    position = np.searchsorted(calendar, frame["date"].to_numpy())

    last_fold = int(np.nanmax(fold_ids))
    eval_end = last_fold if unlock_holdout else min(config.eval_end_fold, last_fold)
    if unlock_holdout:
        logger.warning("Holdout unlocked — folds through %s are in play", eval_end)

    for index, start in enumerate(
        range(config.burn_in_folds + 1, eval_end + 1, config.refit_every)
    ):
        end = min(start + config.refit_every - 1, eval_end)
        is_valid = (fold_ids >= start) & (fold_ids <= end)
        if not is_valid.any():
            continue

        valid_start = position[is_valid].min()
        valid_end = position[is_valid].max()

        # Purge: a training row's label window [t, t+horizon] must close before the
        # validation window opens. Embargo: or it must start well after that window
        # closed, because serial correlation carries information backwards.
        before = position + config.horizon < valid_start
        after = position > valid_end + config.embargo
        is_train = (before | after) & ~is_valid

        yield Fold(
            index=index,
            train_idx=np.flatnonzero(is_train),
            valid_idx=np.flatnonzero(is_valid),
            valid_start_fold=start,
            valid_end_fold=end,
        )
