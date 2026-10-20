"""Inference CLI.

    python -m src.inference.cli decide [-c CONFIG] [--version latest-narrow] [--asof DATE]
    python -m src.inference.cli replay [-c CONFIG] [--version latest-narrow]
"""

import argparse
import logging
import sys
from pathlib import Path

from src.inference.replay import run_replay
from src.inference.resolve import DEFAULT_VERSION
from src.inference.run import DEFAULT_CONFIG, run_decide

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AURUM inference: BUY / SELL / HOLD")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_ in (
        ("decide", "Score the newest date and emit decisions"),
        ("replay", "Backtest the decision rule on the holdout"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--config", "-c", default=DEFAULT_CONFIG)
        p.add_argument("--version", default=DEFAULT_VERSION, help="Registry version or alias")
        if name == "decide":
            p.add_argument("--asof", default=None, help="Score as of this date (skips staleness guard)")
            p.add_argument("--out", default="decisions", help="Directory for the CSV output")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        if args.command == "decide":
            run_decide(args.config, args.version, args.asof, Path(args.out))
        else:
            run_replay(args.config, args.version)
    except (RuntimeError, ValueError, FileNotFoundError) as error:
        logger.error("%s", error)
        sys.exit(1)


if __name__ == "__main__":
    main()
