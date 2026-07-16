"""Core settings: secrets from env/.env, logging bootstrap."""

import logging

from pydantic_settings import BaseSettings, SettingsConfigDict


class CoreConfig(BaseSettings):
    """Platform-wide settings. Secrets live in env/.env only, never YAML."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_url: str | None = None
    sec_user_agent: str | None = None


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
