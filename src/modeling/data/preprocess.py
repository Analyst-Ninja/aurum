"""The contract between the warehouse and the model.

One rule governs this module: **every step must be replayable against
``gold.mart_features`` alone**, which has no target columns. So there is no imputation
from training statistics, no fitted scaling, and no filter that looks at the outcome.
``build_features`` is the single entry point, used by training and inference alike;
training writes the manifest, inference replays it.

The layer is thin on purpose. GOLD already winsorizes, z-scores, deciles and
sector-centres per date — repeating any of that here would be wrong twice over.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.modeling.config import PreprocessConfig

logger = logging.getLogger(__name__)

# Anything a model must never see, however it got there. Checked as a last line of
# defence after the deny-lists have been applied, because a leaked target is silent:
# it produces a model that scores beautifully and is worthless.
LEAKAGE_PATTERN = re.compile(r"^(fwd_ret|label_)")

PREPROCESS_MANIFEST = "preprocess_manifest.json"
FEATURE_MANIFEST = "feature_manifest.json"


def filter_rows(
    frame: pd.DataFrame, target: str, config: PreprocessConfig
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Apply the three row filters in order, counting each.

    Tradability (``close_raw >= 1``, ``adv_21d >= 1e6``) is already applied inside
    ``mart_training_set`` and is deliberately not repeated.
    """
    filters: list[dict[str, Any]] = []

    def record(name: str, param: Any, before: int, after: int) -> None:
        filters.append({"name": name, "param": param, "dropped": before - after})
        logger.info("filter %s(%s) dropped %s rows", name, param, before - after)

    # 1. The last few trading dates have no forward return yet — the right edge of
    #    the panel, guaranteed NULL by tests/assert_targets_null_at_edge.sql.
    before = len(frame)
    frame = frame[frame[target].notna()]
    record("null_target", target, before, len(frame))

    # 2. Burn in each symbol's first year, where the 252-day feature family is
    #    computed over a partial window.
    before = len(frame)
    bar_number = frame.groupby("symbol", observed=True)["date"].rank(method="first")
    frame = frame[bar_number > config.warmup_bars]
    record("warmup_burnin", config.warmup_bars, before, len(frame))

    # 3. A guard, not an active filter: the thinnest date in the panel carries 311
    #    symbols. It stays because the universe is configurable, and the manifest
    #    logs the zero — a guard that has never fired should say so.
    before = len(frame)
    per_date = frame.groupby("date", observed=True)["symbol"].transform("size")
    frame = frame[per_date >= config.min_cross_section]
    record("min_cross_section", config.min_cross_section, before, len(frame))

    return frame.reset_index(drop=True), filters


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Add ``has_fundamentals``.

    34.6% of rows have no fundamental data at all. Without an explicit indicator the
    model cannot tell *stale* from *missing*; with it, and with the existing
    ``days_since_available``, it can condition on both.
    """
    frame = frame.copy()
    frame["has_fundamentals"] = frame["fundamental_available_from"].notna()
    return frame


def transform_target(
    frame: pd.DataFrame, target: str, config: PreprocessConfig
) -> pd.DataFrame:
    """Winsorize and standardize the target per date. Training only.

    Both steps are per date, and neither exists at inference — the target does not
    either. Standardizing by the date's cross-sectional dispersion stops 2008 and
    2020 contributing several times the squared error of a calm year, which would
    quietly turn this into a crisis model.

    ``clip`` is used rather than a hand-rolled ``np.maximum`` because it propagates
    NaN. Postgres ``greatest``/``least`` ignore NULLs and clamp them to a percentile;
    reproducing that here would silently invent targets.
    """
    frame = frame.copy()
    grouped = frame.groupby("date", observed=True)[target]
    lower = grouped.transform(lambda s: s.quantile(config.winsorize_lower))
    upper = grouped.transform(lambda s: s.quantile(config.winsorize_upper))
    frame[target] = frame[target].clip(lower=lower, upper=upper)

    if config.standardize_per_date:
        deviation = frame.groupby("date", observed=True)[target].transform("std")
        # np.nan, not pd.NA: dividing a float column by a pd.NA-bearing one yields an
        # object column whose truthiness then raises "boolean value of NA is ambiguous".
        frame[target] = frame[target] / deviation.replace(0.0, np.nan)
    return frame


def build_features(
    frame: pd.DataFrame,
    config: PreprocessConfig,
    manifest: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Return the feature matrix and the manifest describing it.

    Training calls this without a manifest and writes the one it gets back. Inference
    passes the stored manifest, and any difference in membership or order raises —
    a silently reordered feature matrix produces confident, meaningless predictions.
    """
    denied = {
        "leakage": config.leakage_columns,
        "identifier": config.identifier_columns,
        "non_stationary": config.non_stationary_columns,
    }
    dropped = {column for group in denied.values() for column in group}
    features = [column for column in frame.columns if column not in dropped]

    leaked = [
        column
        for column in features
        if LEAKAGE_PATTERN.match(column) or column == "fold_id"
    ]
    if leaked:
        raise ValueError(f"Target columns reached the feature matrix: {leaked}")

    if manifest is not None:
        expected = manifest["features"]
        if features != expected:
            missing = [c for c in expected if c not in features]
            extra = [c for c in features if c not in expected]
            raise ValueError(
                "Feature matrix does not match the manifest "
                f"(missing={missing}, unexpected={extra}, "
                f"order_changed={sorted(features) == sorted(expected)})"
            )

    matrix = frame[features].copy()
    categorical = [c for c in config.categorical_columns if c in matrix.columns]
    for column in categorical:
        matrix[column] = matrix[column].astype("category")

    built = {
        "features": features,
        "dtypes": {c: str(dtype) for c, dtype in matrix.dtypes.items()},
        "categorical": categorical,
        "nan_policy": "native",
        "denied": denied,
    }
    return matrix, built


def write_manifests(
    directory: Path, preprocess: dict[str, Any], features: dict[str, Any]
) -> None:
    """Write both manifests as plain JSON."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, payload in ((PREPROCESS_MANIFEST, preprocess), (FEATURE_MANIFEST, features)):
        (directory / name).write_text(json.dumps(payload, indent=2, default=str))
        logger.info("Wrote %s", directory / name)


def read_manifest(path: Path) -> dict[str, Any]:
    """Read a manifest written by :func:`write_manifests`."""
    return json.loads(path.read_text())
