"""AURUM entrypoint: run the incremental OHLCV pipeline."""

from sqlalchemy import create_engine

from src.core.config import CoreConfig, setup_logging
from src.core.sinks import PostgresSink
from src.core.state import LandingTableStateStore
from src.datasources.apis.yahoo.config import YahooConfig
from src.datasources.apis.yahoo.ohlcv_ds import YahooOhlcvDataSource
from src.datasources.apis.yahoo.symbols import get_sec_symbols
from src.pipelines.ohlcv_daily import OhlcvDailyPipeline


def main():
    core, yahoo = CoreConfig(), YahooConfig()
    setup_logging()
    postgres_url = core.postgres_url
    if postgres_url is not None:
        engine_factory = lambda: create_engine(postgres_url, echo=False)
    else:
        engine_factory = None
    pipeline = OhlcvDailyPipeline(
        source=YahooOhlcvDataSource(yahoo),
        state=LandingTableStateStore(engine_factory, yahoo.table),
        sink=PostgresSink(engine_factory),
        config=yahoo,
    )
    pipeline.run(get_sec_symbols(core.sec_user_agent))


if __name__ == "__main__":
    main()
