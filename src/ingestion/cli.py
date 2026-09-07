import argparse
import logging
import sys
from datetime import datetime

from src.ingestion.runner import run_feed

TRUE_VALUES = {"true", "t", "yes", "y", "1"}
FALSE_VALUES = {"false", "f", "no", "n", "0"}


def str_to_bool(value: str) -> bool:
    """Parse an explicit True/False argument value."""
    normalised = value.strip().lower()
    if normalised in TRUE_VALUES:
        return True
    if normalised in FALSE_VALUES:
        return False
    raise argparse.ArgumentTypeError(
        f"Expected True or False for --full_load, got {value!r}"
    )


def main():
    parser = argparse.ArgumentParser(description='Ingestion CLI')
    parser.add_argument("--config", "-c", required=True, help="Path to config file")
    parser.add_argument("--run_date", "-d", default=datetime.now().strftime("%Y-%m-%d"), help="Run date")
    parser.add_argument(
        "--full_load",
        "-f",
        type=str_to_bool,
        default=True,
        metavar="True|False",
        help="Full load (default: True). Pass -f False to run incrementally from the watermark.",
    )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    metrics = run_feed(args.config, args.run_date, args.full_load)

    # BaseFeed.run() catches everything and reports the failure in the metrics dict
    # rather than raising, so without this the process exits 0 on a broken feed and an
    # orchestrator (Step Functions, cron) records a green run. SUCCESS_NO_DATA is not a
    # failure — a market holiday legitimately returns no rows.
    if metrics.get("execution_status") == "FAILED":
        logging.error(
            "Feed failed: %s", metrics.get("error_message", "no error message recorded")
        )
        sys.exit(1)

if __name__ == "__main__":
    main()