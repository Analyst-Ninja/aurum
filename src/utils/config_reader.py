from pathlib import Path
from typing import Dict, Any

import yaml


def read_config(config_path: Path) -> Dict[str, Any]:
    with open(config_path, "r") as f:
        return yaml.safe_load(f)
