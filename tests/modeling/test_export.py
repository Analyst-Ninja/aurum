"""`export` publishes a finished run to S3 (#75).

The client is faked rather than mocked against moto: the contract under test is the key
layout and the file selection, and a fake that records `put_object` calls states that
directly without a new dev dependency.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.modeling.config import ExportConfig
from src.modeling.export.s3 import BUCKET_ENV, resolve_bucket, upload_run

NOW = datetime(2026, 9, 7, 6, 18, 46, tzinfo=UTC)
STAMP = "2026-09-07T061846Z"


class FakeS3:
    """Records what would have been written."""

    def __init__(self):
        self.calls = []

    def put_object(self, **kwargs):
        self.calls.append(kwargs)

    @property
    def keys(self):
        return [call["Key"] for call in self.calls]


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """A registry with one version and a `latest-narrow` symlink pointing at it."""
    version = tmp_path / "20260907-5030979-narrow"
    (version / "backtest").mkdir(parents=True)
    (version / "shap").mkdir()
    (version / "model.txt").write_text("tree")
    (version / "metrics.json").write_text("{}")
    (version / "shap" / "ranking.csv").write_text("feature_name\n")
    (version / "backtest" / "report.html").write_text("<html></html>")
    (version / "backtest" / "positions.parquet").write_bytes(b"PAR1")
    (tmp_path / "latest-narrow").symlink_to(version.name)
    return tmp_path


@pytest.fixture(autouse=True)
def bucket_env(monkeypatch):
    monkeypatch.setenv(BUCKET_ENV, "aurum-artifacts-123")


def test_keys_are_prefix_version_timestamp_path(run_dir):
    client = FakeS3()
    upload_run(run_dir, "20260907-5030979-narrow", ExportConfig(), client, NOW)

    assert set(client.keys) == {
        f"runs/20260907-5030979-narrow/{STAMP}/model.txt",
        f"runs/20260907-5030979-narrow/{STAMP}/metrics.json",
        f"runs/20260907-5030979-narrow/{STAMP}/shap/ranking.csv",
        f"runs/20260907-5030979-narrow/{STAMP}/backtest/report.html",
    }


def test_symlink_resolves_to_the_real_version_id(run_dir):
    """`latest-narrow` moves every month; the key must name the run that produced it."""
    client = FakeS3()
    keys = upload_run(run_dir, "latest-narrow", ExportConfig(), client, NOW)

    assert all(key.startswith(f"runs/20260907-5030979-narrow/{STAMP}/") for key in keys)
    assert not any("latest" in key for key in keys)


def test_excluded_files_are_not_uploaded(run_dir):
    client = FakeS3()
    keys = upload_run(run_dir, "latest-narrow", ExportConfig(), client, NOW)

    assert not any(key.endswith("positions.parquet") for key in keys)


def test_exclusion_list_is_configurable(run_dir):
    """Dropping model.txt is a one-line config change, not a code change."""
    config = ExportConfig(exclude=["model.txt", "backtest/positions.parquet"])
    client = FakeS3()
    keys = upload_run(run_dir, "latest-narrow", config, client, NOW)

    assert not any(key.endswith("model.txt") for key in keys)
    assert f"runs/20260907-5030979-narrow/{STAMP}/backtest/report.html" in keys


def test_report_is_uploaded_as_html_so_a_browser_renders_it(run_dir):
    client = FakeS3()
    upload_run(run_dir, "latest-narrow", ExportConfig(), client, NOW)

    report = next(c for c in client.calls if c["Key"].endswith("report.html"))
    assert report["ContentType"] == "text/html"
    assert report["Body"] == b"<html></html>"


def test_missing_run_raises_rather_than_uploading_nothing(run_dir):
    client = FakeS3()
    config = ExportConfig()

    with pytest.raises(FileNotFoundError):
        upload_run(run_dir, "latest-full", config, client, NOW)
    assert client.calls == []


def test_env_bucket_wins_over_the_config(monkeypatch):
    monkeypatch.setenv(BUCKET_ENV, "from-terraform")
    assert resolve_bucket(ExportConfig(bucket="from-yaml")) == "from-terraform"


def test_config_bucket_is_the_fallback(monkeypatch):
    monkeypatch.delenv(BUCKET_ENV, raising=False)
    assert resolve_bucket(ExportConfig(bucket="from-yaml")) == "from-yaml"


def test_no_bucket_anywhere_raises_naming_the_env_var(monkeypatch):
    monkeypatch.delenv(BUCKET_ENV, raising=False)
    config = ExportConfig()

    with pytest.raises(ValueError, match=BUCKET_ENV):
        resolve_bucket(config)
