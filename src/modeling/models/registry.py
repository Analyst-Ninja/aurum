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
import re
import subprocess
from datetime import date
from importlib.metadata import version
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DBT_MANIFEST = Path("src/transformation/aurum_dwh/target/manifest.json")
TRACKED_PACKAGES = ("lightgbm", "pandas", "numpy", "pyarrow")

# A suffix becomes both a path segment and a symlink name, so it is checked rather than
# trusted: without this, `--version-suffix ../../etc` writes outside the registry.
SUFFIX_PATTERN = re.compile(r"^[a-z0-9-]+$")


def _validated_suffix(suffix: str | None) -> str | None:
    if suffix is None:
        return None
    if not SUFFIX_PATTERN.fullmatch(suffix):
        raise ValueError(
            f"Version suffix must match {SUFFIX_PATTERN.pattern}, got {suffix!r}"
        )
    return suffix


def git_sha(short: bool = False) -> str:
    """The commit this run came from, or ``unknown`` outside a repo.

    ``AURUM_GIT_SHA`` wins when set. The training container has neither git nor a
    ``.git`` directory (both excluded from the image), so without the override every
    containerised run would be stamped ``unknown`` — and two of them on the same day
    would share a version id and overwrite each other. Compose passes the value
    through; see docs/operations/training-container.md.
    """
    override = os.getenv("AURUM_GIT_SHA")
    if override:
        return override[:7] if short else override

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


def version_id(suffix: str | None = None) -> str:
    """``{YYYYMMDD}-{git short sha}``, plus ``-{suffix}`` when one is given.

    The suffix exists because the weekly pipeline trains twice — once on every feature,
    once on the SHAP-narrowed set — from one image on one day. Without it both runs
    resolve to the same id and the second overwrites the first, leaving `compare` to
    measure a run against itself. Suffixing rather than perturbing the sha keeps the sha
    a real commit, which is the whole point of the ``AURUM_GIT_SHA`` override.
    """
    base = f"{date.today():%Y%m%d}-{git_sha(short=True)}"
    suffix = _validated_suffix(suffix)
    return f"{base}-{suffix}" if suffix else base


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


def _point_latest_at(directory: Path, name: str = "latest") -> None:
    """Repoint ``models/{name}`` atomically.

    Written to a temporary name and renamed, so a reader never sees a moment with no
    symlink at all.
    """
    latest = directory.parent / name
    staging = directory.parent / f".{name}.{os.getpid()}"
    staging.unlink(missing_ok=True)
    staging.symlink_to(directory.name)
    os.replace(staging, latest)


def save_run(
    root: Path,
    booster: Any,
    metadata: dict[str, Any],
    feature_manifest: dict[str, Any],
    preprocess_manifest: dict[str, Any],
    suffix: str | None = None,
) -> Path:
    """Write ``models/{version}/`` and repoint ``models/latest`` at it.

    With a suffix, ``models/latest-{suffix}`` is repointed as well. That second symlink
    is what lets an orchestrator name a run without reconstructing its id: Step Functions
    has no date formatting, so `latest-full` and `latest-narrow` are the only stable
    handles the weekly state machine can use.

    Plain ``latest`` still moves on every run, suffix or not. It means "most recently
    trained", not "promoted" — promotion stays a human decision.
    """
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

    if suffix := _validated_suffix(suffix):
        _point_latest_at(directory, name=f"latest-{suffix}")
        logger.info("models/latest-%s -> %s", suffix, directory.name)

    return directory


def load_run(root: Path, version_name: str = "latest") -> tuple[Any, dict[str, Any]]:
    """Load a saved booster and its feature manifest."""
    import lightgbm as lgb

    directory = root / version_name
    booster = lgb.Booster(model_file=str(directory / "model.txt"))
    manifest = json.loads((directory / "feature_manifest.json").read_text())
    return booster, manifest
