"""Yahoo OHLCV settings: configs/yahoo.yaml defaults, YAHOO_* env overrides."""

from datetime import date
from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

_YAML = Path(__file__).resolve().parents[4] / "configs" / "yahoo.yaml"


class YahooConfig(BaseSettings):
    model_config = SettingsConfigDict(
        yaml_file=_YAML, env_prefix="YAHOO_", extra="ignore"
    )

    interval: str = "1d"
    batch_size: int = 100
    sleep_seconds: int = 10
    history_floor: date = date(2000, 1, 1)
    table: str = "ohlcv_1d"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Priority (first wins): init args > env > .env > YAML > secrets
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )
