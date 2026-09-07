"""The factory is the only thing standing between a config typo and an import error.

A `type` the registry has never seen must name itself in the message — the decorators
only fire when the module is imported, so "not supported" almost always means a feed
was added without the matching import in `runner.py`.
"""

import pytest

from src.ingestion.factory import factory
from src.ingestion.factory.registory import DATASOURCE_REGISTRY, FEED_REGISTRY


@pytest.fixture
def registered(monkeypatch):
    """A throwaway type in both registries, removed again after the test."""

    class _Thing:
        def __init__(self, config):
            self.config = config

    monkeypatch.setitem(DATASOURCE_REGISTRY, "test_source", _Thing)
    monkeypatch.setitem(FEED_REGISTRY, "test_feed", _Thing)
    return _Thing


def test_datasource_is_built_from_the_registry_and_handed_its_config(registered):
    config = {"type": "test_source", "table": "ohlcv_1d"}

    built = factory.create_datasource(config)

    assert isinstance(built, registered)
    assert built.config is config


def test_feed_is_built_from_the_registry_and_handed_its_config(registered):
    config = {"type": "test_feed", "name": "ohlcv"}

    built = factory.create_feed(config)

    assert isinstance(built, registered)
    assert built.config is config


@pytest.mark.parametrize("config", [{}, {"type": ""}])
def test_a_missing_datasource_type_is_rejected(config):
    with pytest.raises(ValueError, match="must include a type"):
        factory.create_datasource(config)


@pytest.mark.parametrize("config", [{}, {"type": ""}])
def test_a_missing_feed_type_is_rejected(config):
    with pytest.raises(ValueError, match="must include a type"):
        factory.create_feed(config)


def test_an_unregistered_datasource_names_the_type():
    with pytest.raises(ValueError, match="snowflake not supported"):
        factory.create_datasource({"type": "snowflake"})


def test_an_unregistered_feed_names_the_type():
    """Almost always a feed module missing from runner.py's import list."""
    with pytest.raises(ValueError, match="news_sentiment not supported"):
        factory.create_feed({"type": "news_sentiment"})


def test_the_shipped_types_are_registered_by_importing_runner():
    from src.ingestion import runner  # noqa: F401  — the imports are the registration

    assert "yahoo_ohlcv" in DATASOURCE_REGISTRY
    assert "edgar_financials" in DATASOURCE_REGISTRY
    assert "postgres" in DATASOURCE_REGISTRY
    assert "ohlcv_1d" in FEED_REGISTRY
    assert "income_stmts_quarterly" in FEED_REGISTRY
