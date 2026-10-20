"""Paper-trading CLI.

    python -m src.paper.cli rebalance [--dry-run] [--force] [--asof DATE]
    python -m src.paper.cli snapshot  [--watch]
    python -m src.paper.cli report    [-p W,M,Q,Y]
    python -m src.paper.cli backtest  --start 2024-01-01 --end 2026-10-01 [--bar-interval 30Min] [-p W,M,Q,Y]
    python -m src.paper.cli crosscheck --start ... --end ... [--engines sim,vectorbt,backtrader,lumibot]
"""

import argparse
import logging
import sys

from src.paper import report
from src.paper.config import DEFAULT_PATH, PaperConfig, load_paper_config

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AURUM paper trading")
    parser.add_argument("--config", "-c", default=DEFAULT_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("rebalance", help="Score, then trade toward the target book")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="Run with the market closed")
    p.add_argument("--asof", default=None)
    p = sub.add_parser("snapshot", help="Record equity and exposure")
    p.add_argument("--watch", action="store_true", help="Repeat every snapshot_interval_min while open")
    for name in ("report", "backtest", "crosscheck"):
        p = sub.add_parser(name)
        p.add_argument("--periods", "-p", default=None, help="Comma list of W,M,Q,Y")
        if name != "report":
            p.add_argument("--start", required=True)
            p.add_argument("--end", required=True)
            p.add_argument("--bar-interval", default=None)
            p.add_argument("--cost-bps", type=float, default=None)
            p.add_argument("--model-version", default=None, help="Registry version or alias (default from config); `latest` uses the newest local run")
            p.add_argument("--scores-file", default=None, help="Parquet of date, symbol, score instead of scoring the holdout")
            p.add_argument("--bars-file", default=None, help="Parquet of symbol, timestamp, open, close instead of fetching from Alpaca")
            if name == "crosscheck":
                p.add_argument("--engines", default="sim,vectorbt,backtrader,lumibot")
    return parser


def _periods(args, config: PaperConfig) -> list[str]:
    return [x.strip().upper() for x in args.periods.split(",")] if args.periods else config.report_periods


def main() -> None:
    args = _parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    config = load_paper_config(args.config)
    try:
        if args.command == "rebalance":
            from src.paper.live import rebalance

            rebalance(config, args.asof, args.dry_run, args.force)
        elif args.command == "snapshot":
            from src.paper.live import snapshot, watch

            watch(config) if args.watch else print(snapshot(config))
        elif args.command == "report":
            from src.paper.ledger import Ledger

            snaps = Ledger(config.ledger_url).snapshots()
            if snaps.empty:
                raise ValueError("No snapshots yet; run `snapshot` first")
            print(report.render(snaps["equity"], _periods(args, config), config.capital, snaps["benchmark"]))
        else:
            from src.paper.backtesting.runner import run_all

            if args.bar_interval:
                config.bar_interval = args.bar_interval
            if args.model_version:
                config.model_version = args.model_version
            if args.cost_bps is not None:
                config.cost_bps = args.cost_bps
            engines = ["sim"] if args.command == "backtest" else [e.strip() for e in args.engines.split(",")]
            run_all(config, args.start, args.end, engines, _periods(args, config), args.scores_file, args.bars_file)
    except (RuntimeError, ValueError, FileNotFoundError) as error:
        logger.error("%s", error)
        sys.exit(1)


if __name__ == "__main__":
    main()
