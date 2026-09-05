"""Modelling CLI. One YAML config fully specifies a run.

Only ``train`` does anything yet, and only as far as loading the frame — fitting
arrives with GH-53. The remaining subcommands are declared so the interface is
visible, and fail loudly naming the issue that implements them.
"""

import argparse
import logging

from src.modeling.config import load_config
from src.modeling.data.loader import load_training_frame
from src.modeling.data.preprocess import (
    add_indicators,
    build_features,
    filter_rows,
    transform_target,
    write_manifests,
)
from src.modeling.data.splits import walk_forward_folds

logger = logging.getLogger(__name__)

# Subcommands whose implementation lands in a later issue.
PENDING = {
    "evaluate": "GH-54",
    "select-features": "GH-55",
    "backtest": "GH-56",
    "predict": "GH-53",
}


def train(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    frame = load_training_frame(config)
    rows_in = len(frame)
    logger.info(
        "Loaded %s rows x %s cols, target %s",
        f"{rows_in:,}",
        frame.shape[1],
        config.target,
    )

    frame, filters = filter_rows(frame, config.target, config.preprocess)
    frame = add_indicators(frame)
    frame = transform_target(frame, config.target, config.preprocess)
    matrix, feature_manifest = build_features(frame, config.preprocess)
    logger.info("%s rows x %s features", f"{len(matrix):,}", matrix.shape[1])

    folds = list(walk_forward_folds(frame, config.splits))
    logger.info(
        "%s folds, validating fold_id %s-%s",
        len(folds),
        folds[0].valid_start_fold,
        folds[-1].valid_end_fold,
    )

    write_manifests(
        config.output_dir,
        {
            "source_table": f"{config.source.db_schema}.{config.source.table}",
            "rows_in": rows_in,
            "filters": filters,
            "rows_out": len(frame),
            "target": config.target,
            "target_transforms": ["winsorize_1_99_per_date", "standardize_per_date"],
        },
        feature_manifest,
    )
    logger.info("Fitting lands in GH-53")


def main() -> None:
    parser = argparse.ArgumentParser(description="Modelling CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("train", *PENDING):
        subparser = subparsers.add_parser(name)
        subparser.add_argument(
            "--config", "-c", required=True, help="Path to run config YAML"
        )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    if args.command == "train":
        train(args)
    else:
        raise NotImplementedError(f"{args.command} lands in {PENDING[args.command]}")


if __name__ == "__main__":
    main()
