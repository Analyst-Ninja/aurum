from typing import Dict

from src.ingestion.factory.registory import FEED_REGISTRY, DATASOURCE_REGISTRY


def create_datasource(config: Dict):
    """Factory method to create datasource based on config"""

    ds_type = config.get("type", "")
    if not ds_type:
        raise ValueError("Datasource must include a type")

    if ds_type not in DATASOURCE_REGISTRY:
        raise ValueError(f"Datasource type {ds_type} not supported")

    ds_class = DATASOURCE_REGISTRY[ds_type]
    return ds_class(config)

def create_feed(config: Dict):
    """Factory method to create feed based on config"""

    feed_type = config.get("type", "")
    if not feed_type:
        raise ValueError("Feed type must include a type")

    if feed_type not in FEED_REGISTRY:
        raise ValueError(f"Feed type {feed_type} not supported")

    feed_class = FEED_REGISTRY[feed_type]
    return feed_class(config)

