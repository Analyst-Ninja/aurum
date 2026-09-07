from pathlib import Path
from typing import Dict, Any

import yaml

from src.utils.paths import safe_path


def read_config(config_path: Path) -> Dict[str, Any]:
    # The path comes from a CLI argument; safe_path rejects anything that resolves
    # outside the project (or a temp dir) before it is opened.
    with open(safe_path(config_path), "r") as f:
        return yaml.safe_load(f)
