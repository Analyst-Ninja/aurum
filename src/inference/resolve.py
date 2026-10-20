"""Find the run to serve: a local registry directory, else the newest one on S3.

The training task publishes every run to ``s3://{bucket}/{prefix}/{version}/{timestamp}/``
(``modeling.export.s3``). A host with no EFS mount — the EC2 box — therefore reads the
model from there. ``latest-narrow`` is a symlink that exists only beside the trainer, so
off S3 the alias is resolved by version id instead; ids start with the run date, which
makes the lexicographic maximum the newest.

S3 is read with aiobotocore (aiohttp underneath), so the lookup can overlap the database
read instead of queueing ahead of it. Local file writes are tiny and go through a thread.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from src.modeling.config import ExportConfig
from src.modeling.export.s3 import resolve_bucket

logger = logging.getLogger(__name__)

DEFAULT_VERSION = "latest-narrow"
# Everything `load_run` and the decision caveat read; the heavy folds file is not needed.
# model.txt last: it is what `_is_run` tests, so an interrupted fetch is retried rather than
# mistaken for a complete run.
RUN_FILES = ("metadata.json", "feature_manifest.json", "preprocess_manifest.json", "model.txt")
OPTIONAL_FILES = ("backtest/summary.json",)


def _is_run(directory: Path) -> bool:
    return (directory / "model.txt").is_file()


def _matches(alias: str, version: str) -> bool:
    """Whether version id ``version`` is what the alias or id ``alias`` asks for."""
    if alias == "latest":
        return True
    if alias.startswith("latest-"):
        return version.endswith(f"-{alias.removeprefix('latest-')}")
    return version == alias


def _newest_local(root: Path, alias: str) -> Path | None:
    candidates = sorted(
        (d for d in root.glob("2*") if d.is_dir() and _is_run(d) and _matches(alias, d.name)),
        key=lambda d: d.name,
    )
    return candidates[-1] if candidates else None


@asynccontextmanager
async def _s3_client() -> AsyncIterator[Any]:  # pragma: no cover - exercised only against real S3
    from aiobotocore.session import get_session

    async with get_session().create_client("s3") as client:
        yield client


async def _prefixes(client: Any, bucket: str, prefix: str) -> list[str]:
    """Immediate sub-"directories" under ``prefix``, as bare names."""
    names: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    async for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        names += [p["Prefix"].removeprefix(prefix).strip("/") for p in page.get("CommonPrefixes", [])]
    return names


def _write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


async def _fetch(client: Any, bucket: str, key: str) -> bytes | None:
    """The object's bytes, or ``None`` when the key does not exist."""
    try:
        response = await client.get_object(Bucket=bucket, Key=key)
    except client.exceptions.NoSuchKey:
        return None
    async with response["Body"] as stream:
        return await stream.read()


async def _download_newest(root: Path, alias: str, export: ExportConfig, client: Any) -> Path:
    bucket = resolve_bucket(export)
    base = f"{export.prefix.strip('/')}/"
    versions = sorted(v for v in await _prefixes(client, bucket, base) if _matches(alias, v))
    if not versions:
        raise FileNotFoundError(f"No run matching {alias!r} under s3://{bucket}/{base}")
    version = versions[-1]

    directory = root / version
    if _is_run(directory):
        return directory

    stamps = sorted(await _prefixes(client, bucket, f"{base}{version}/"))
    if not stamps:
        raise FileNotFoundError(f"s3://{bucket}/{base}{version}/ holds no export")
    source = f"{base}{version}/{stamps[-1]}/"

    # Independent objects, so fetched together. Written only after every required one
    # arrived, model.txt last: it is what `_is_run` tests, so a failure leaves nothing
    # that looks like a complete run.
    names = (*RUN_FILES, *OPTIONAL_FILES)
    bodies = await asyncio.gather(*(_fetch(client, bucket, source + n) for n in names))
    for name, body in zip(names, bodies):
        if body is None and name in RUN_FILES:
            raise FileNotFoundError(f"s3://{bucket}/{source}{name} is missing")
    for name, body in zip(names, bodies):
        if body is not None:
            await asyncio.to_thread(_write, directory / name, body)
    logger.info("Fetched %s from s3://%s/%s", version, bucket, source)
    return directory


async def resolve_run(
    root: Path,
    version: str = DEFAULT_VERSION,
    export: ExportConfig | None = None,
) -> Path:
    """Directory of the run named ``version`` (a version id or a ``latest*`` alias).

    Order: the registry entry itself (a symlink beside the trainer), then S3 when a
    bucket is configured, then the newest matching local directory.
    """
    direct = (root / version).resolve()
    if _is_run(direct):
        return direct

    export = export or ExportConfig()
    try:
        resolve_bucket(export)
    except ValueError:
        local = _newest_local(root, version)
        if local is None:
            raise FileNotFoundError(
                f"No run {version!r} under {root} and no artifacts bucket configured"
            ) from None
        return local.resolve()

    async with _s3_client() as client:
        return (await _download_newest(root, version, export, client)).resolve()
