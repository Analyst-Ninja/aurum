"""Score a saved run and write ``models/{version}/metrics.json``.

The split of what counts is the one #54 insists on: **every headline number comes from
the 24-month holdout**, scored once with the shipped booster. The per-fold numbers are
from folds 121-297, which chose the hyperparameters and the iteration count, and are
labelled ``model_selection`` in the output so nobody quotes them as out-of-sample.

A model is only interesting relative to something. Four baselines run over the exact
same rows — a zero prediction, a momentum sort, a reversal sort and a Ridge on the `_z`
columns — because a gradient-boosted 193-feature model that ties `mom_12_1_z` has told
us that momentum works, not that the model does.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.modeling.config import ModelingConfig, load_config
from src.modeling.data.loader import load_training_frame
from src.modeling.data.preprocess import add_indicators, build_features, filter_rows
from src.modeling.evaluate import metrics
from src.modeling.models.registry import load_run

logger = logging.getLogger(__name__)

FOLD_PREDICTIONS = "fold_predictions.parquet"
METRICS = "metrics.json"

# The regime label for the volatility breakdown. It is in `NON_STATIONARY_COLUMNS` and
# so never a feature — it is identical across every symbol on a date and carries no
# ranking information — but that is exactly what makes it a clean regime label.
REGIME_COLUMN = "market_vol_63d"
REGIME_LABELS = ["low_vol", "mid_vol", "high_vol"]

# Known limits of these numbers, written into the output rather than left in a doc
# nobody opens next to the file.
CAVEATS = [
    "Headline numbers are the 24-month holdout; fold 121-297 numbers chose the "
    "hyperparameters and are model-selection scores, not out-of-sample results.",
    "Decile spread and Sharpe are gross of transaction costs. GH-56's cost sweep "
    "decides whether they survive.",
    "r2 is computed against the raw forward return while the regression head was "
    "fitted on the per-date standardized target, so its level is a scale artefact and "
    "is not comparable between heads. The rank and return metrics are unaffected.",
    "walk_forward_folds builds its training mask as (before | after), so each fold "
    "trains on rows from after its own validation window. Fold-level numbers inherit "
    "that; the holdout block does not, because the shipped model is a pre-holdout "
    "refit. Tracked separately from GH-54.",
]


def _panel(
    config: ModelingConfig, manifest: dict[str, Any]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load and rebuild the exact matrix the run was trained on.

    Deliberately *not* `cli._prepare`: that calls `transform_target`, which winsorizes
    and standardizes the target per date. Standardized units are right for fitting and
    wrong for reporting — a decile spread has to be in returns, not in multiples of the
    day's cross-sectional dispersion. Rank metrics are unaffected either way.

    The source table also stays `mart_training_set`. `predict` swaps in
    `predict_table`, which by construction has no targets and so cannot be scored.
    """
    frame = load_training_frame(config)
    frame, _ = filter_rows(frame, config.target, config.preprocess)
    frame = add_indicators(frame)
    matrix, _ = build_features(frame, config.preprocess, manifest=manifest)
    logger.info("Panel %s rows x %s features", f"{len(frame):,}", matrix.shape[1])
    return frame, matrix


def _block(frame: pd.DataFrame, pred: np.ndarray, target: str, horizon: int) -> dict:
    """Metric block for one set of predictions over `frame`'s rows."""
    return metrics.summarize(
        frame[target].to_numpy(dtype="float64"),
        np.asarray(pred, dtype="float64"),
        frame["date"].to_numpy(),
        frame["symbol"].to_numpy(),
        horizon,
    )


def _baselines(
    holdout: pd.DataFrame,
    pre_holdout: pd.DataFrame,
    features: list[str],
    target: str,
    horizon: int,
) -> dict:
    """The four controls, scored on the same holdout rows as the model."""
    from sklearn.linear_model import Ridge

    z_columns = [name for name in features if name.endswith("_z")]

    # A zero prediction has no ordering at all, so its IC is undefined rather than
    # zero — every date drops out. It is here for the return-side numbers and as the
    # floor any tradeable signal has to clear.
    blocks = {
        "zero": _block(holdout, np.zeros(len(holdout)), target, horizon),
        "momentum_mom_12_1_z": _block(
            holdout, holdout["mom_12_1_z"].to_numpy(), target, horizon
        ),
        "reversal_5d_z": _block(
            holdout, holdout["reversal_5d_z"].to_numpy(), target, horizon
        ),
    }

    # Ridge is the "is a linear model enough?" control. NaNs are filled with zero for
    # this baseline only — the columns are per-date z-scores, so zero is that date's
    # mean and the least informative fill available. LightGBM routes NaN natively and
    # needs no such thing; that difference is part of what is being measured.
    ridge = Ridge(alpha=1.0)
    ridge.fit(
        pre_holdout[z_columns].astype("float32").fillna(0.0),
        pre_holdout[target].astype("float64"),
    )
    prediction = ridge.predict(holdout[z_columns].astype("float32").fillna(0.0))
    blocks["ridge_z_features"] = _block(holdout, prediction, target, horizon) | {
        "n_features": len(z_columns),
        "nan_policy": "filled with 0.0 (the per-date mean of a z-score)",
    }
    return blocks


def _breakdowns(holdout: pd.DataFrame, pred: np.ndarray, target: str, horizon: int) -> dict:
    """Per-sector and per-volatility-regime slices of the holdout.

    An aggregate IC can hide a model that works in energy and loses money in tech, or
    one that only works when the market is calm. Both slices are mandatory in
    `docs/modeling/modeling-design.md` §5.4.
    """
    scored = holdout.assign(_pred=pred)

    by_sector = {
        str(sector): _block(rows, rows["_pred"].to_numpy(), target, horizon)
        for sector, rows in scored.groupby("sector", observed=True)
        if len(rows) > horizon
    }

    # Terciles of the market-level series, cut over distinct dates rather than rows so
    # a date with more listed names does not pull the boundaries.
    per_date = scored.groupby("date", observed=True)[REGIME_COLUMN].first().dropna()
    regime = pd.qcut(per_date, 3, labels=REGIME_LABELS, duplicates="drop")
    scored["_regime"] = scored["date"].map(regime)
    by_regime = {
        str(label): _block(rows, rows["_pred"].to_numpy(), target, horizon)
        for label, rows in scored.groupby("_regime", observed=True)
        if len(rows) > horizon
    }

    return {"sector": by_sector, "volatility_regime": by_regime}


def _secondary_heads(
    matrix: pd.DataFrame,
    frame: pd.DataFrame,
    is_holdout: np.ndarray,
    params: dict[str, Any],
    n_estimators: int,
    target: str,
    horizon: int,
) -> dict:
    """Classification and ranking heads, fitted once and scored on the holdout.

    #54 asks for these on the same folds as the regression head. Fitted once on the
    pre-holdout panel instead: thirty extra fits on 2.5M rows would buy fold-level
    numbers the issue itself labels model-selection-only, and what the comparison is
    actually for — does a different objective on the same features rank better than
    regression? — is answered on the holdout. Recorded as a deviation.
    """
    import lightgbm as lgb

    params = {
        key: value
        for key, value in params.items()
        if key not in ("objective", "n_estimators", "early_stopping_rounds")
    }
    test = matrix[is_holdout]
    holdout = frame[is_holdout]
    heads: dict[str, Any] = {
        "deviation": (
            "fitted once on the pre-holdout panel and scored on the holdout, not "
            "refitted per fold"
        )
    }

    labels = frame["label_up_5d"]
    usable = labels.notna().to_numpy() & ~is_holdout
    classifier = lgb.LGBMClassifier(
        objective="binary", n_estimators=n_estimators, **params
    )
    classifier.fit(matrix[usable], labels[usable].astype("int8"))
    heads["classifier_label_up_5d"] = _block(
        holdout, classifier.predict_proba(test)[:, 1], target, horizon
    )

    # LambdaRank needs its rows grouped by query — here, by date — and contiguous, so
    # the training rows are reordered and the group sizes are the per-date counts.
    grades = frame["fwd_ret_5d_xs_decile"]
    usable = grades.notna().to_numpy() & ~is_holdout
    order = np.argsort(frame.loc[usable, "date"].to_numpy(), kind="stable")
    ranked_rows = matrix[usable].iloc[order]
    ranked_labels = (grades[usable].iloc[order].astype("int8") - 1).clip(lower=0)
    groups = frame.loc[usable, "date"].iloc[order].value_counts(sort=False).sort_index()

    ranker = lgb.LGBMRanker(
        objective="lambdarank", n_estimators=n_estimators, **params
    )
    ranker.fit(ranked_rows, ranked_labels, group=groups.to_numpy())
    heads["ranker_fwd_ret_5d_xs_decile"] = _block(
        holdout, ranker.predict(test), target, horizon
    )
    return heads


def _fold_block(
    directory: Path, metadata: dict[str, Any], target: str, horizon: int
) -> dict:
    """Per-fold metrics from the saved out-of-sample predictions.

    Falls back to the IC recorded in `metadata.json` when the parquet is missing — runs
    trained before #54 have no fold predictions, and that should degrade the report
    rather than block it.
    """
    path = directory / FOLD_PREDICTIONS
    if not path.exists():
        logger.warning(
            "%s absent — per-fold detail limited to the IC in metadata.json. "
            "Retrain to populate it.",
            path,
        )
        return {
            "purpose": "model_selection",
            "source": "metadata.json",
            "note": "retrain to get full per-fold metrics",
            "per_fold": [
                {
                    "fold_index": fold["fold_index"],
                    "valid_start_fold": fold["valid_start_fold"],
                    "valid_end_fold": fold["valid_end_fold"],
                    "mean_ic": fold["best_ic"],
                }
                for fold in metadata.get("folds", [])
            ],
        }

    predictions = pd.read_parquet(path)
    per_fold = [
        {
            "fold_index": int(index),
            **_block(rows.rename(columns={"y": target}), rows["pred"].to_numpy(), target, horizon),
        }
        for index, rows in predictions.groupby("fold_index", observed=True)
    ]
    pooled = _block(
        predictions.rename(columns={"y": target}),
        predictions["pred"].to_numpy(),
        target,
        horizon,
    )
    return {
        "purpose": "model_selection",
        "source": FOLD_PREDICTIONS,
        "per_fold": per_fold,
        "pooled": pooled,
    }


def evaluate(config_path: str, version: str) -> Path:
    """Score `version` and write its ``metrics.json``. Returns the file path."""
    config = load_config(config_path)
    booster, manifest = load_run(config.output_dir, version)
    directory = (config.output_dir / version).resolve()
    metadata = json.loads((directory / "metadata.json").read_text())

    target, horizon = config.target, config.splits.horizon
    frame, matrix = _panel(config, manifest)

    is_holdout = (frame["fold_id"] > config.splits.eval_end_fold).to_numpy()
    if not is_holdout.any():
        raise ValueError(
            f"No rows past fold {config.splits.eval_end_fold} — nothing to evaluate"
        )
    holdout = frame[is_holdout]
    prediction = booster.predict(matrix[is_holdout])
    logger.info(
        "Holdout %s rows, %s dates",
        f"{len(holdout):,}",
        holdout["date"].nunique(),
    )

    report = {
        "version": metadata.get("version", version),
        "evaluated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": metadata.get("git_sha"),
        "target": target,
        "horizon": horizon,
        "holdout": {
            "purpose": "headline",
            "starts_at_fold": config.splits.eval_end_fold + 1,
            "start_date": str(holdout["date"].min().date()),
            "end_date": str(holdout["date"].max().date()),
            "model": _block(holdout, prediction, target, horizon),
            "baselines": _baselines(
                holdout, frame[~is_holdout], manifest["features"], target, horizon
            ),
            "breakdowns": _breakdowns(holdout, prediction, target, horizon),
            "secondary_heads": _secondary_heads(
                matrix,
                frame,
                is_holdout,
                metadata["params"],
                metadata["final_n_estimators"],
                target,
                horizon,
            ),
        },
        "folds": _fold_block(directory, metadata, target, horizon),
        "caveats": CAVEATS,
    }

    path = directory / METRICS
    path.write_text(json.dumps(report, indent=2, default=str))
    headline = report["holdout"]["model"]
    logger.info(
        "Holdout IC %.4f, ICIR %.2f, decile spread %.4f, LS Sharpe %.2f",
        headline["ic"]["mean"],
        headline["icir"],
        headline["decile_spread"]["mean"],
        headline["long_short_sharpe"],
    )
    logger.info("Wrote %s", path)
    return path
