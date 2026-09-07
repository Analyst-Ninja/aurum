"""Copy a finished run's artifacts to S3.

EFS is where the pipeline writes and where the next step reads; S3 is where a human
reads. The two are not interchangeable — reaching an EFS access point means starting a
task in the VPC, so without this step the only way to see a backtest report is to run a
container just to cat a file.

The key layout is ``{prefix}/{version}/{timestamp}/{path within the run}``. The version
id already encodes the run date and the commit (``20260907-5030979-narrow``), so the
timestamp only has to separate two exports of the same version, which is what a re-run
produces.
"""

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.modeling.config import ExportConfig

logger = logging.getLogger(__name__)

BUCKET_ENV = "AURUM_ARTIFACTS_BUCKET"

# Colons are legal in an S3 key but need quoting in a shell and escaping in a URL, so
# the ISO form is flattened rather than used verbatim.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H%M%SZ"

# Enough for the browser to render report.html from a presigned URL rather than offering
# it as a download. Everything absent from this map is left to S3's default.
CONTENT_TYPES = {
    ".html": "text/html",
    ".json": "application/json",
    ".csv": "text/csv",
    ".png": "image/png",
}


def resolve_bucket(config: ExportConfig) -> str:
    """The config's bucket, else ``AURUM_ARTIFACTS_BUCKET``.

    Environment first so Terraform owns the real name: the bucket is suffixed with the
    account id, and a config committed to git must not carry one.
    """
    bucket = os.getenv(BUCKET_ENV) or config.bucket
    if not bucket:
        raise ValueError(
            f"No artifacts bucket configured. Set ${BUCKET_ENV} or export.bucket."
        )
    return bucket


def _files_to_upload(directory: Path, exclude: list[str]) -> list[Path]:
    """Every file under `directory`, minus the exclusions, in a stable order."""
    excluded = {directory / name for name in exclude}
    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and path not in excluded
    )


def upload_run(
    root: Path,
    version: str,
    config: ExportConfig,
    client: Any = None,
    now: datetime | None = None,
) -> list[str]:
    """Upload ``root/version`` to S3 and return the keys written.

    `version` may be a symlink name — `latest-narrow` is what the pipeline passes, since
    Step Functions cannot reconstruct a version id. The symlink is resolved before it
    reaches the key, so the artifacts are filed under the run that produced them rather
    than under a name that moves every month.
    """
    directory = (root / version).resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"No run at {root / version}")

    bucket = resolve_bucket(config)
    if client is None:  # pragma: no cover - exercised only against real S3
        import boto3

        client = boto3.client("s3")

    stamp = (now or datetime.now(UTC)).strftime(TIMESTAMP_FORMAT)
    base = f"{config.prefix.strip('/')}/{directory.name}/{stamp}"

    keys = []
    for path in _files_to_upload(directory, config.exclude):
        key = f"{base}/{path.relative_to(directory).as_posix()}"
        extra = {}
        if content_type := CONTENT_TYPES.get(path.suffix):
            extra["ContentType"] = content_type
        client.put_object(Bucket=bucket, Key=key, Body=path.read_bytes(), **extra)
        keys.append(key)

    logger.info("Uploaded %s files to s3://%s/%s/", len(keys), bucket, base)
    return keys
