import argparse
import logging
from datetime import datetime

from src.ingestion.runner import run_feed

def main():
    parser = argparse.ArgumentParser(description='Ingestion CLI')
    parser.add_argument("--config", "-c", required=True, help="Path to config file")
    parser.add_argument("--run_date", "-d", default=datetime.now().strftime("%Y-%m-%d"), help="Run date")
    parser.add_argument("--full_load", "-f", default=False, action="store_true", help="Full load")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    run_feed(args.config, args.run_date, args.full_load)

if __name__ == "__main__":
    main()