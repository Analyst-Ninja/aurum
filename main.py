"""AURUM entrypoint: run the incremental OHLCV pipeline."""

from sqlalchemy import create_engine

from src.core.config import CoreConfig, setup_logging
from src.core.sinks import PostgresSink
from src.core.state import LandingTableStateStore
from src.datasources.apis.yahoo.config import OHLCVDailyConfig, OHLCVMinuteConfig
from src.datasources.apis.yahoo.ohlcv_ds import YahooOhlcvDataSource
from src.datasources.apis.yahoo.symbols import get_snp500_symbols
from src.pipelines.ohlcv_daily import StockMarketDataPipeline


def main():
    core = CoreConfig()
    ohlcv_daily = OHLCVDailyConfig()
    ohlcv_minute = OHLCVMinuteConfig()
    setup_logging()
    engine_factory = (
        (lambda: create_engine(core.postgres_url, echo=False))
        if core.postgres_url
        else None
    )
    ohlcv_daily_pipeline = StockMarketDataPipeline(
        source=YahooOhlcvDataSource(ohlcv_daily),
        state=LandingTableStateStore(engine_factory, ohlcv_daily.table),
        sink=PostgresSink(engine_factory),
        config=ohlcv_daily,
    )
    # ohlcv_daily_pipeline.run(get_sec_symbols(core.sec_user_agent))
    ohlcv_daily_pipeline.run(get_snp500_symbols(core.sec_user_agent))

    ohlcv_1min_pipeline = StockMarketDataPipeline(
        source=YahooOhlcvDataSource(ohlcv_minute),
        state=LandingTableStateStore(engine_factory, ohlcv_minute.table),
        sink=PostgresSink(engine_factory),
        config=ohlcv_minute,
    )
    # ohlcv_1min_pipeline.run(get_sec_symbols(core.sec_user_agent))
    ohlcv_1min_pipeline.run(get_snp500_symbols(core.sec_user_agent))


if __name__ == "__main__":
    main()
