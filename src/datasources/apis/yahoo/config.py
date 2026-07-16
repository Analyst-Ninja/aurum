"""Yahoo OHLCV settings: configs/yahoo.yaml defaults, YAHOO_* env overrides."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

OHLCV_DAILY_YAML = Path(__file__).resolve().parents[4] / "configs" / "ohlcv_1d.yaml"
OHLCV_MINUTE_YAML = Path(__file__).resolve().parents[4] / "configs" / "ohlcv_1min.yaml"


def _seven_days_ago() -> date:
    """History floor default: 7 days before now (UTC), evaluated per instantiation."""
    return (datetime.now(timezone.utc) - timedelta(days=7)).date()


class OHLCVMinuteConfig(BaseSettings):
    model_config = SettingsConfigDict(
        yaml_file=OHLCV_MINUTE_YAML, env_prefix="YAHOO_", extra="ignore"
    )

    interval: str = "1m"
    batch_size: int = 100
    sleep_seconds: int = 10
    history_floor: date = Field(default_factory=_seven_days_ago)
    table: str = "ohlcv_1min"

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


class OHLCVDailyConfig(BaseSettings):
    model_config = SettingsConfigDict(
        yaml_file=OHLCV_DAILY_YAML, env_prefix="YAHOO_", extra="ignore"
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
