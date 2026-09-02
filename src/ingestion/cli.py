import argparse
import logging
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

    run_feed(args.config, args.run_date, args.full_load)

if __name__ == "__main__":
    main()