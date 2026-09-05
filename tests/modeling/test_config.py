from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.modeling.config import load_config

VALID = {
    "source": {
        "db_schema": "gold",
        "table": "mart_training_set",
        "db_name": "aurum",
        "host": "HOST",
        "port": "PORT",
        "username": "AURUM_USERNAME",
        "password": "AURUM_PASSWORD",
    },
    "target": "fwd_ret_5d_excess",
}


def write_config(tmp_path: Path, config: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def test_valid_config_round_trips(tmp_path):
    config = load_config(write_config(tmp_path, VALID))

    assert config.source.table == "mart_training_set"
    assert config.target == "fwd_ret_5d_excess"
    # Cache section is optional and defaults.
    assert config.cache.enabled is True
    assert config.cache.dir == Path("data/training")


def test_unknown_key_raises_naming_the_key(tmp_path):
    path = write_config(tmp_path, {**VALID, "targt": "fwd_ret_5d"})

    with pytest.raises(ValidationError, match="targt"):
        load_config(path)


def test_unknown_nested_key_raises_naming_the_key(tmp_path):
    config = {**VALID, "source": {**VALID["source"], "schema": "gold"}}

    with pytest.raises(ValidationError, match="schema"):
        load_config(write_config(tmp_path, config))


def test_missing_required_key_raises(tmp_path):
    config = {**VALID, "source": {k: v for k, v in VALID["source"].items() if k != "table"}}

    with pytest.raises(ValidationError, match="table"):
        load_config(write_config(tmp_path, config))
