"""Modelling CLI. One YAML config fully specifies a run.

Only ``train`` does anything yet, and only as far as loading the frame — fitting
arrives with GH-53. The remaining subcommands are declared so the interface is
visible, and fail loudly naming the issue that implements them.
"""

import argparse
import logging

from src.modeling.config import load_config
from src.modeling.data.loader import load_training_frame

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
    logger.info(
        "Loaded %s rows x %s cols, target %s",
        f"{len(frame):,}",
        frame.shape[1],
        config.target,
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
