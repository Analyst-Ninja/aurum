"""Run configuration for the modelling subsystem.

One YAML fully specifies a run, mirroring the ingestion framework. Credentials are
env var *names*, resolved at connect time from the repo-root ``.env``.

Every model forbids extra keys, so a mistyped one fails at load with the offending
key named rather than being silently ignored.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from src.utils.config_reader import read_config


class SourceConfig(BaseModel):
    """The warehouse table a run reads from."""

    model_config = ConfigDict(extra="forbid")

    db_schema: str
    table: str
    db_name: str
    # Env var NAMES, not values.
    host: str
    port: str
    username: str
    password: str


class CacheConfig(BaseModel):
    """Local Parquet cache of the source table."""

    model_config = ConfigDict(extra="forbid")

    dir: Path = Path("data/training")
    enabled: bool = True


class ModelingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: SourceConfig
    target: str
    cache: CacheConfig = CacheConfig()


def load_config(config_path: str | Path) -> ModelingConfig:
    """Read and validate a run config."""
    return ModelingConfig(**read_config(Path(config_path)))
