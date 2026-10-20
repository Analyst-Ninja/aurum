"""Paper-trading config: one YAML, secrets referenced by env-var name."""

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from src.utils.env import load_env

DEFAULT_PATH = "src/paper/configs/default.yaml"


@dataclass
class PaperConfig:
    capital: float = 100_000
    long_only: bool = False
    leg_exposure: float = 0.5
    horizon: int = 5
    rebalance_every_n_days: int = 1
    snapshot_interval_min: int = 30
    min_order_usd: float = 50
    cost_bps: float = 5
    execution_time: str = "09:35"
    bar_interval: str = "30Min"
    benchmark: str = "SPY"
    report_periods: list[str] = field(default_factory=lambda: ["W", "M", "Q", "Y"])
    model_version: str = "latest-narrow"
    model_config: str = "src/modeling/configs/lgbm_xs_excess_5d_narrow.yaml"
    ledger_url: str = "sqlite:///data/paper/ledger.db"
    out_dir: str = "data/paper"
    alpaca_key: str = "ALPACA_API_KEY"
    alpaca_secret: str = "ALPACA_SECRET_KEY"
    feed: str = "iex"

    def credentials(self) -> tuple[str, str]:
        load_env()
        key, secret = os.getenv(self.alpaca_key), os.getenv(self.alpaca_secret)
        if not key or not secret:
            raise ValueError(f"{self.alpaca_key} / {self.alpaca_secret} are not set in .env")
        return key, secret


def load_paper_config(path: str = DEFAULT_PATH) -> PaperConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    known = {f.name for f in fields(PaperConfig)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Unknown paper config keys: {sorted(unknown)}")
    return PaperConfig(**raw)
