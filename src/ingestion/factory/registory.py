from typing import Callable, Type, Dict

from src.ingestion.datasources.base_datasource import BaseDatasource
from src.ingestion.feed.base_feed import BaseFeed

DATASOURCE_REGISTRY: Dict[str, Type[BaseDatasource]] = {}
FEED_REGISTRY: Dict[str, Type[BaseFeed]] = {}

def register_datasource(name: str) -> Callable:
    """A decorator to register a datasource"""

    def decorator(cls: Type[BaseDatasource]) -> Callable:
        DATASOURCE_REGISTRY[name] = cls
        return cls

    return decorator

def register_feed(name: str) -> Callable:
    """A decorator to register a feed"""
    def decorator(cls: Type[BaseFeed]) -> Callable:
        FEED_REGISTRY[name] = cls
        return cls
    return decorator


