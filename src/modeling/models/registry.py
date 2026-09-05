"""Flat-file model registry.

No MLflow — spec §7 Q5 defers it until a retraining cadence makes a running service
worth its keep. A directory per run, named for the date and the commit that produced
it, is reviewable in a pull request and needs nothing running.

The split of what gets committed is deliberate: `model.txt` and other bulk are
gitignored, while `metadata.json` is tracked, so a change in a run's provenance or
(from #54) its metrics shows up as a git diff rather than in a directory nobody opens.
"""

import hashlib
import json
import logging
import os
import subprocess
from datetime import date
from importlib.metadata import version
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DBT_MANIFEST = Path("src/transformation/aurum_dwh/target/manifest.json")
TRACKED_PACKAGES = ("lightgbm", "pandas", "numpy", "pyarrow")


def git_sha(short: bool = False) -> str:
    """The commit this run came from, or ``unknown`` outside a repo."""
    # `--short` is a flag, not a substitute for the revision: `git rev-parse --short`
    # without HEAD exits 128, so every short call fell into the `unknown` branch and
    # every run of a day shared the version id `{date}-unknown` — and silently
    # overwrote the previous one, model.txt included.
    args = ["git", "rev-parse", *(["--short"] if short else []), "HEAD"]
    try:
        return subprocess.run(
            args, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def version_id() -> str:
    """``{YYYYMMDD}-{git short sha}``."""
    return f"{date.today():%Y%m%d}-{git_sha(short=True)}"


def file_hash(path: Path) -> str | None:
    """sha256 of a file, or None when it is absent."""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config_hash(config: Any) -> str:
    """sha256 of the run config, so two runs can be compared without diffing YAML."""
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def package_versions() -> dict[str, str]:
    return {name: version(name) for name in TRACKED_PACKAGES}


def _point_latest_at(directory: Path) -> None:
    """Repoint ``models/latest`` atomically.

    Written to a temporary name and renamed, so a reader never sees a moment with no
    ``latest`` at all.
    """
    latest = directory.parent / "latest"
    staging = directory.parent / f".latest.{os.getpid()}"
    staging.unlink(missing_ok=True)
    staging.symlink_to(directory.name)
    os.replace(staging, latest)


def save_run(
    root: Path,
    booster: Any,
    metadata: dict[str, Any],
    feature_manifest: dict[str, Any],
    preprocess_manifest: dict[str, Any],
) -> Path:
    """Write ``models/{version}/`` and repoint ``models/latest`` at it."""
    directory = root / metadata["version"]
    directory.mkdir(parents=True, exist_ok=True)

    # Native text format, not a pickle: a pickled sklearn wrapper is bound to the
    # library version that made it, and this repo will outlive its lightgbm pin.
    booster.save_model(str(directory / "model.txt"))
    for name, payload in (
        ("metadata.json", metadata),
        ("feature_manifest.json", feature_manifest),
        ("preprocess_manifest.json", preprocess_manifest),
    ):
        (directory / name).write_text(json.dumps(payload, indent=2, default=str))

    _point_latest_at(directory)
    logger.info("Saved run to %s (models/latest -> %s)", directory, directory.name)
    return directory


def load_run(root: Path, version_name: str = "latest") -> tuple[Any, dict[str, Any]]:
    """Load a saved booster and its feature manifest."""
    import lightgbm as lgb

    directory = root / version_name
    booster = lgb.Booster(model_file=str(directory / "model.txt"))
    manifest = json.loads((directory / "feature_manifest.json").read_text())
    return booster, manifest
