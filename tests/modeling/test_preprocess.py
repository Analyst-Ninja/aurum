import numpy as np
import pandas as pd
import pytest

from src.modeling.config import PreprocessConfig
from src.modeling.data.preprocess import (
    add_indicators,
    build_features,
    filter_rows,
    read_manifest,
    transform_target,
    write_manifests,
)

CONFIG = PreprocessConfig(warmup_bars=2, min_cross_section=2)


def _panel(symbols=("AAPL", "MSFT"), days=6, target="fwd_ret_5d_excess"):
    """A small panel shaped like mart_training_set."""
    dates = pd.date_range("2026-01-01", periods=days, freq="D")
    rows = []
    for symbol in symbols:
        for i, date in enumerate(dates):
            rows.append(
                {
                    "symbol": symbol,
                    "date": date,
                    "sector": "Tech",
                    "industry": "Software",
                    "period_type": "Q",
                    "fiscal_period_end": date,
                    "fundamental_available_from": date if i % 2 else pd.NaT,
                    "ret_21d": 0.01 * (i + 1),
                    "market_cap": 1e10,
                    "market_cap_z": 0.5,
                    target: 0.01 * (i + 1),
                    "fold_id": 1,
                }
            )
    return pd.DataFrame(rows)


def test_filters_drop_exactly_what_they_claim():
    frame = _panel()
    frame.loc[0, "fwd_ret_5d_excess"] = np.nan

    filtered, filters = filter_rows(frame, "fwd_ret_5d_excess", CONFIG)

    by_name = {f["name"]: f for f in filters}
    assert by_name["null_target"]["dropped"] == 1
    # Two bars per symbol. Burn-in ranks the rows that survived the target filter, so
    # AAPL — whose first bar went with the null — burns its next two instead.
    assert by_name["warmup_burnin"]["dropped"] == 4
    assert len(filtered) == len(frame) - sum(f["dropped"] for f in filters)


def test_min_cross_section_is_a_guard_that_reports_its_zero():
    # It drops nothing on a full panel, and the manifest must still say so — a guard
    # that has never fired should be visible, not absent.
    _, filters = filter_rows(_panel(), "fwd_ret_5d_excess", CONFIG)

    guard = next(f for f in filters if f["name"] == "min_cross_section")
    assert guard["dropped"] == 0
    assert guard["param"] == 2


def test_min_cross_section_drops_a_thin_date():
    frame = _panel()
    frame = frame[~((frame["symbol"] == "MSFT") & (frame["date"] == frame["date"].max()))]

    _, filters = filter_rows(frame, "fwd_ret_5d_excess", CONFIG)

    assert next(f for f in filters if f["name"] == "min_cross_section")["dropped"] == 1


def test_add_indicators_marks_rows_without_fundamentals():
    frame = add_indicators(_panel())

    assert frame["has_fundamentals"].tolist()[:4] == [False, True, False, True]


def test_transform_target_standardizes_per_date_and_keeps_nan():
    frame = _panel(symbols=("A", "B", "C", "D"), days=3)
    frame.loc[0, "fwd_ret_5d_excess"] = np.nan
    # One date carries an outlier that winsorizing must pull in.
    frame.loc[frame.index[-1], "fwd_ret_5d_excess"] = 99.0

    result = transform_target(frame, "fwd_ret_5d_excess", CONFIG)

    assert np.isnan(result.loc[0, "fwd_ret_5d_excess"])
    assert result["fwd_ret_5d_excess"].max() < 99.0
    per_date_std = result.groupby("date")["fwd_ret_5d_excess"].std().dropna()
    assert np.allclose(per_date_std, 1.0)


def test_build_features_drops_every_deny_list():
    matrix, manifest = build_features(add_indicators(_panel()), CONFIG)

    assert "fwd_ret_5d_excess" not in matrix.columns  # leakage
    assert "fold_id" not in matrix.columns  # leakage
    assert "symbol" not in matrix.columns  # identifier
    assert "fiscal_period_end" not in matrix.columns  # identifier, and a datetime
    assert "market_cap" not in matrix.columns  # non-stationary level
    # The stationary counterpart of the dropped level survives.
    assert "market_cap_z" in matrix.columns
    assert manifest["features"] == list(matrix.columns)


def test_build_features_raises_when_a_target_reaches_the_matrix():
    # The acceptance criterion: deliberately smuggle a target past the deny-list.
    config = PreprocessConfig(leakage_columns=["fwd_ret_5d"])

    with pytest.raises(ValueError, match="fwd_ret_5d_excess"):
        build_features(_panel(), config)


def test_build_features_raises_on_a_smuggled_fold_id():
    config = PreprocessConfig(leakage_columns=["fwd_ret_5d_excess"])

    with pytest.raises(ValueError, match="fold_id"):
        build_features(_panel(), config)


def test_the_decile_trap():
    """37 columns end in `_decile`; the 37th is a target.

    Any rule that keeps or drops "the decile columns" by suffix either leaks the
    answer or discards a feature, and both failures are silent.
    """
    frame = _panel()
    for i in range(36):
        frame[f"feature_{i}_decile"] = 1.0
    frame["fwd_ret_5d_xs_decile"] = 1.0

    matrix, _ = build_features(frame, CONFIG)

    deciles = [c for c in matrix.columns if c.endswith("_decile")]
    assert len(deciles) == 36
    assert "fwd_ret_5d_xs_decile" not in deciles


def test_sector_and_industry_become_categorical():
    matrix, manifest = build_features(_panel(), CONFIG)

    assert manifest["categorical"] == ["sector", "industry"]
    assert matrix["sector"].dtype == "category"


def test_inference_frame_replays_the_training_manifest():
    """mart_features is mart_training_set minus the targets; the same call must
    produce the same ordered columns."""
    training = add_indicators(_panel())
    _, manifest = build_features(training, CONFIG)

    inference = training.drop(columns=["fwd_ret_5d_excess", "fold_id"])
    matrix, _ = build_features(inference, CONFIG, manifest=manifest)

    assert list(matrix.columns) == manifest["features"]


def test_replay_raises_when_the_column_order_changed():
    training = add_indicators(_panel())
    _, manifest = build_features(training, CONFIG)
    reordered = training[list(reversed(training.columns))]

    with pytest.raises(ValueError, match="does not match the manifest"):
        build_features(reordered, CONFIG, manifest=manifest)


def test_replay_raises_when_a_feature_is_missing():
    training = add_indicators(_panel())
    _, manifest = build_features(training, CONFIG)

    with pytest.raises(ValueError, match="missing=\\['ret_21d'\\]"):
        build_features(training.drop(columns=["ret_21d"]), CONFIG, manifest=manifest)


def test_manifests_round_trip(tmp_path):
    _, feature_manifest = build_features(_panel(), CONFIG)
    write_manifests(tmp_path, {"rows_in": 12, "filters": []}, feature_manifest)

    assert read_manifest(tmp_path / "feature_manifest.json") == feature_manifest
    assert read_manifest(tmp_path / "preprocess_manifest.json")["rows_in"] == 12
