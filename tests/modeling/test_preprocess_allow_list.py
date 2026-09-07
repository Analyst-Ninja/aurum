"""The allow-list — the narrowed run's only difference from the baseline.

It is intersected *after* the deny-lists, which is what makes a poisoned
`selected_features.csv` able to narrow the matrix but never to widen it back onto a
target column.
"""

import pandas as pd
import pytest

from src.modeling.config import PreprocessConfig
from src.modeling.data.preprocess import build_features


@pytest.fixture
def frame():
    return pd.DataFrame(
        {
            "symbol": ["AAPL"],
            "date": [pd.Timestamp("2024-01-02")],
            "mom_12_1_z": [0.5],
            "reversal_5d_z": [-0.2],
            "value_score_z": [0.1],
            "adj_close": [190.0],
            "fwd_ret_5d_excess": [0.01],
            "fold_id": [300],
        }
    )


def test_the_matrix_follows_the_allow_lists_order(frame):
    config = PreprocessConfig(allow_list=["value_score_z", "mom_12_1_z"])

    matrix, manifest = build_features(frame, config)

    assert list(matrix.columns) == ["value_score_z", "mom_12_1_z"]
    assert manifest["allow_list"] == ["value_score_z", "mom_12_1_z"]


def test_a_name_the_panel_does_not_carry_is_skipped(frame):
    config = PreprocessConfig(allow_list=["mom_12_1_z", "not_a_column"])

    matrix, _ = build_features(frame, config)

    assert list(matrix.columns) == ["mom_12_1_z"]


def test_an_allow_list_cannot_readmit_a_denied_target(frame):
    """A poisoned seed narrows the matrix; it must not be able to widen it."""
    config = PreprocessConfig(allow_list=["fwd_ret_5d_excess", "mom_12_1_z"])

    matrix, _ = build_features(frame, config)

    assert list(matrix.columns) == ["mom_12_1_z"]


def test_an_allow_list_that_matches_nothing_raises_rather_than_training_on_nothing(frame):
    config = PreprocessConfig(allow_list=["adj_close", "fold_id"])

    with pytest.raises(ValueError, match="allow_list matched no columns"):
        build_features(frame, config)
