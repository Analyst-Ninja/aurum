import logging
from pathlib import Path

from src.ingestion.factory import factory
from src.ingestion.feed.base_feed import BaseFeed
from src.utils.config_reader import read_config
from src.ingestion.feed import stock_market, income_stmts
from src.ingestion.datasources.api.yahoo import ohlcv
from src.ingestion.datasources.api.edgar import income_stmts
from src.ingestion.datasources.storage import db

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

def run_feed(config_path: str, run_date: str, full_load: bool = False):
    """Runner for ingestion"""

    logger.info("Starting ingestion")
    logger.info(f"Config Path: {config_path}")

    config = read_config(Path(config_path))

    feed = factory.create_feed(config)

    if isinstance(feed, BaseFeed):
        logger.info("Starting ingestion")
        feed.run(run_date, full_load)

    else:
        raise TypeError(f"Feed type {type(feed).__name__} is not supported")

    print("\n--------------------------")
    logger.info("Finished ingestion")
    print("\n--------------------------")