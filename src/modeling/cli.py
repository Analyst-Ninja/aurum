"""Modelling CLI. One YAML config fully specifies a run.

``train`` and ``predict`` are implemented; the rest are declared so the interface is
visible and fail loudly naming the issue that implements them.
"""

import argparse
import gc
import logging

import numpy as np

from src.modeling.config import ModelingConfig, load_config
from src.modeling.data.loader import load_training_frame
from src.modeling.data.preprocess import (
    add_indicators,
    build_features,
    filter_rows,
    transform_target,
)
from src.modeling.data.splits import walk_forward_folds
from src.modeling.models.lgbm import build_dataset, fit_final, fit_fold
from src.modeling.models.registry import (
    DBT_MANIFEST,
    config_hash,
    file_hash,
    git_sha,
    load_run,
    package_versions,
    save_run,
    version_id,
)

logger = logging.getLogger(__name__)

# Subcommands whose implementation lands in a later issue.
PENDING = {
    "evaluate": "GH-54",
    "select-features": "GH-55",
    "backtest": "GH-56",
}


def _prepare(config: ModelingConfig):
    """Load, filter and build the feature matrix. Shared by train and predict."""
    frame = load_training_frame(config)
    rows_in = len(frame)
    logger.info("Loaded %s rows x %s cols", f"{rows_in:,}", frame.shape[1])

    frame, filters = filter_rows(frame, config.target, config.preprocess)
    frame = add_indicators(frame)
    frame = transform_target(frame, config.target, config.preprocess)
    matrix, feature_manifest = build_features(frame, config.preprocess)
    logger.info("%s rows x %s features", f"{len(matrix):,}", matrix.shape[1])

    preprocess_manifest = {
        "source_table": f"{config.source.db_schema}.{config.source.table}",
        "rows_in": rows_in,
        "filters": filters,
        "rows_out": len(frame),
        "target": config.target,
        "target_transforms": ["winsorize_1_99_per_date", "standardize_per_date"],
    }
    return frame, matrix, feature_manifest, preprocess_manifest


def train(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    frame, matrix, feature_manifest, preprocess_manifest = _prepare(config)

    folds = list(walk_forward_folds(frame, config.splits))
    logger.info(
        "%s folds, validating fold_id %s-%s",
        len(folds),
        folds[0].valid_start_fold,
        folds[-1].valid_end_fold,
    )

    # Everything still needed after binning, kept as narrow columns rather than by
    # holding the whole frame: the dates for the IC eval, and the pre-holdout mask.
    dates = frame["date"]
    pre_holdout = np.flatnonzero(
        (frame["fold_id"] <= config.splits.eval_end_fold).to_numpy()
    )
    n_features = matrix.shape[1]

    # Bin once, then let the raw panel go. On a 16GB machine the frame and the matrix
    # together are ~5.5GB, and holding them through fifteen fits is what pushes the
    # machine into swap.
    dataset = build_dataset(matrix, frame[config.target], config.train.grid[0].model_dump())
    del frame, matrix
    gc.collect()

    # One walk-forward pass per configuration; the grid is a list so widening the
    # search is a config change. n_configs_tried is recorded because a result picked
    # from many attempts is not the result you get — #56's deflated Sharpe reads it.
    best_fits, best_params, best_ic = None, None, -np.inf
    for params in config.train.grid:
        fits = [fit_fold(dataset, dates, fold, params.model_dump()) for fold in folds]
        mean_ic = float(np.mean([fit.best_ic for fit in fits]))
        logger.info("config mean IC %.4f", mean_ic)
        if mean_ic > best_ic:
            best_fits, best_params, best_ic = fits, params, mean_ic

    # The fold fits chose the iteration count; the shipped model is refitted on every
    # pre-holdout row, so it has seen the year the last fold had to hold back.
    n_estimators = int(np.median([fit.best_iteration for fit in best_fits]))
    booster = fit_final(dataset, pre_holdout, best_params.model_dump(), n_estimators)

    metadata = {
        "version": version_id(),
        "git_sha": git_sha(),
        "dbt_manifest_hash": file_hash(DBT_MANIFEST),
        "config_hash": config_hash(config),
        "target": config.target,
        "params": best_params.model_dump(),
        "n_configs_tried": len(config.train.grid),
        "mean_validation_ic": best_ic,
        "final_n_estimators": n_estimators,
        "holdout_starts_at_fold": config.splits.eval_end_fold + 1,
        "n_rows": int(len(pre_holdout)),
        "n_features": n_features,
        "package_versions": package_versions(),
        "folds": [
            {
                "fold_index": fit.fold_index,
                "valid_start_fold": fit.valid_start_fold,
                "valid_end_fold": fit.valid_end_fold,
                "train_start_date": fit.train_start_date,
                "train_end_date": fit.train_end_date,
                "valid_start_date": fit.valid_start_date,
                "valid_end_date": fit.valid_end_date,
                "n_train": fit.n_train,
                "n_valid": fit.n_valid,
                "best_iteration": fit.best_iteration,
                "best_ic": fit.best_ic,
            }
            for fit in best_fits
        ],
    }
    directory = save_run(
        config.output_dir, booster, metadata, feature_manifest, preprocess_manifest
    )
    logger.info("Mean validation IC %.4f across %s folds", best_ic, len(best_fits))
    logger.info("Wrote %s", directory)


def predict(args: argparse.Namespace) -> None:
    """Score the latest date with the promoted model.

    Replays the stored feature manifest through the same ``build_features`` used at
    training. A column-order mismatch raises there rather than producing confident
    nonsense here.
    """
    config = load_config(args.config)
    booster, manifest = load_run(config.output_dir, args.version)

    config.source.table = config.source.predict_table
    frame = load_training_frame(config)
    frame = add_indicators(frame)
    if args.asof:
        frame = frame[frame["date"] <= args.asof]
    frame = frame[frame["date"] == frame["date"].max()]

    matrix, _ = build_features(frame, config.preprocess, manifest=manifest)
    scores = booster.predict(matrix)

    ranked = (
        frame[["symbol", "date"]]
        .assign(score=scores)
        .sort_values("score", ascending=False)
    )
    logger.info("Scored %s symbols as of %s", len(ranked), ranked["date"].iloc[0].date())
    print(ranked.head(20).to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Modelling CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("train", "predict", *PENDING):
        subparser = subparsers.add_parser(name)
        subparser.add_argument(
            "--config", "-c", required=True, help="Path to run config YAML"
        )
        if name == "predict":
            subparser.add_argument("--version", default="latest", help="Registry version")
            subparser.add_argument("--asof", default=None, help="Score as of this date")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if args.command == "train":
        train(args)
    elif args.command == "predict":
        predict(args)
    else:
        raise NotImplementedError(f"{args.command} lands in {PENDING[args.command]}")


if __name__ == "__main__":
    main()
