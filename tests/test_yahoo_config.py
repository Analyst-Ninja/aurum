"""YahooConfig: YAML defaults, env override, validation at construction."""

from datetime import date

from src.datasources.apis.yahoo.config import YahooConfig


def test_loads_yaml_defaults():
    cfg = YahooConfig()
    assert cfg.interval == "1d"
    assert cfg.batch_size == 100
    assert cfg.sleep_seconds == 10
    assert cfg.history_floor == date(2000, 1, 1)
    assert cfg.table == "ohlcv_1d"


def test_env_overrides_yaml(monkeypatch):
    monkeypatch.setenv("YAHOO_BATCH_SIZE", "25")
    assert YahooConfig().batch_size == 25
