import numpy as np
import pandas as pd

from src.modeling.config import SplitConfig
from src.modeling.data.splits import sample_weights, walk_forward_folds

HORIZON = 5
EMBARGO = 10


def _panel(n_folds=40, symbols=3, days_per_fold=21):
    """A panel with fold_id dense-ranked over months, as the warehouse builds it."""
    rows = []
    date = pd.Timestamp("2000-01-03")
    for fold in range(1, n_folds + 1):
        for _ in range(days_per_fold):
            for symbol in range(symbols):
                rows.append({"symbol": f"S{symbol}", "date": date, "fold_id": fold})
            date += pd.Timedelta(days=1)
    return pd.DataFrame(rows)


CONFIG = SplitConfig(
    horizon=HORIZON, embargo=EMBARGO, burn_in_folds=10, eval_end_fold=30, refit_every=12
)


def test_no_training_label_window_reaches_the_validation_window():
    """The whole point of purging: a row's [t, t+5] label must close before the
    validation window opens, or its label was partly observed inside it."""
    frame = _panel()
    calendar = np.sort(frame["date"].unique())
    position = np.searchsorted(calendar, frame["date"].to_numpy())

    folds = list(walk_forward_folds(frame, CONFIG))
    assert folds

    for fold in folds:
        valid = position[fold.valid_idx]
        train = position[fold.train_idx]
        before = train[train < valid.min()]
        assert (before + HORIZON < valid.min()).all()


def test_no_training_row_falls_inside_the_embargo():
    frame = _panel()
    calendar = np.sort(frame["date"].unique())
    position = np.searchsorted(calendar, frame["date"].to_numpy())

    for fold in walk_forward_folds(frame, CONFIG):
        valid = position[fold.valid_idx]
        train = position[fold.train_idx]
        after = train[train > valid.max()]
        assert (after > valid.max() + EMBARGO).all()


def test_train_and_validation_never_overlap():
    for fold in walk_forward_folds(_panel(), CONFIG):
        assert not set(fold.train_idx) & set(fold.valid_idx)


def test_burn_in_folds_are_never_validated():
    for fold in walk_forward_folds(_panel(), CONFIG):
        assert fold.valid_start_fold > CONFIG.burn_in_folds


def test_burn_in_folds_are_still_trained_on():
    frame = _panel()
    first = next(iter(walk_forward_folds(frame, CONFIG)))

    trained_folds = frame["fold_id"].to_numpy()[first.train_idx]
    assert trained_folds.min() == 1


def test_the_holdout_is_never_yielded_by_default():
    frame = _panel()

    validated = np.concatenate(
        [frame["fold_id"].to_numpy()[f.valid_idx] for f in walk_forward_folds(frame, CONFIG)]
    )

    assert validated.max() <= CONFIG.eval_end_fold


def test_the_holdout_is_reachable_only_when_unlocked():
    frame = _panel()

    validated = np.concatenate(
        [
            frame["fold_id"].to_numpy()[f.valid_idx]
            for f in walk_forward_folds(frame, CONFIG, unlock_holdout=True)
        ]
    )

    assert validated.max() == frame["fold_id"].max()


def test_the_window_expands():
    sizes = [len(fold.train_idx) for fold in walk_forward_folds(_panel(), CONFIG)]

    assert sizes == sorted(sizes)
    assert len(sizes) > 1


def test_validation_windows_are_contiguous_and_refit_every_applies():
    folds = list(walk_forward_folds(_panel(), CONFIG))

    assert folds[0].valid_start_fold == CONFIG.burn_in_folds + 1
    for fold in folds[:-1]:
        assert fold.valid_end_fold - fold.valid_start_fold + 1 == CONFIG.refit_every
    for earlier, later in zip(folds, folds[1:], strict=False):
        assert later.valid_start_fold == earlier.valid_end_fold + 1


def test_sample_weights_are_uniform_unless_a_half_life_is_set():
    dates = pd.Series(pd.date_range("2020-01-01", periods=10, freq="YE"))

    assert sample_weights(dates, CONFIG) is None

    decayed = sample_weights(dates, SplitConfig(decay_half_life_years=3.0))
    assert decayed[-1] == 1.0
    assert (np.diff(decayed) > 0).all()
