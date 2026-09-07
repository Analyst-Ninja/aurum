import json

import lightgbm as lgb
import numpy as np
import pytest

from src.modeling.config import ModelingConfig, SourceConfig
from src.modeling.models.registry import (
    config_hash,
    file_hash,
    git_sha,
    load_run,
    package_versions,
    save_run,
    version_id,
)


@pytest.fixture
def booster():
    rng = np.random.default_rng(0)
    data = rng.normal(size=(200, 3))
    label = data[:, 0] + rng.normal(scale=0.1, size=200)
    return lgb.train(
        {"objective": "regression", "verbosity": -1, "min_child_samples": 5},
        lgb.Dataset(data, label=label),
        num_boost_round=5,
    )


def _config():
    return ModelingConfig(
        source=SourceConfig(
            db_schema="gold",
            table="mart_training_set",
            db_name="aurum",
            host="HOST",
            port="PORT",
            username="AURUM_USERNAME",
            password="AURUM_PASSWORD",
        ),
        target="fwd_ret_5d_excess",
    )


def _metadata(version="20260905-abc1234"):
    return {"version": version, "git_sha": "abc1234", "folds": []}


def test_version_id_is_date_and_short_sha():
    version = version_id()
    date_part, sha = version.split("-")

    assert len(date_part) == 8
    assert date_part.isdigit()
    assert sha == git_sha(short=True)


def test_version_id_appends_a_suffix():
    assert version_id("narrow") == f"{version_id()}-narrow"


def test_version_id_without_a_suffix_is_unchanged():
    """The suffix is opt-in — existing commands must produce the id they always did."""
    version = version_id(None)

    assert version.count("-") == 1
    assert version.endswith(git_sha(short=True))


@pytest.mark.parametrize("suffix", ["../escape", "with/slash", "UPPER", "has space", ""])
def test_version_id_rejects_an_unsafe_suffix(suffix):
    """The suffix becomes a path segment and a symlink name, so it is validated."""
    with pytest.raises(ValueError):
        version_id(suffix)


def test_suffixed_saves_do_not_overwrite_each_other(tmp_path, booster):
    """The reason this feature exists (GH-78).

    The weekly pipeline trains twice on one day from one image. Without a suffix both
    runs resolve to the same version id and the narrowed run destroys the baseline it is
    supposed to be compared against.
    """
    full = save_run(
        tmp_path, booster, _metadata(version_id("full")), {"features": ["a", "b"]}, {},
        suffix="full",
    )
    narrow = save_run(
        tmp_path, booster, _metadata(version_id("narrow")), {"features": ["a"]}, {},
        suffix="narrow",
    )

    assert full != narrow
    assert (full / "model.txt").exists()
    assert json.loads((full / "feature_manifest.json").read_text())["features"] == ["a", "b"]
    assert (tmp_path / "latest-full").resolve() == full.resolve()
    assert (tmp_path / "latest-narrow").resolve() == narrow.resolve()


def test_a_suffixed_save_still_moves_plain_latest(tmp_path, booster):
    """`latest` means "most recently trained", not "promoted"."""
    directory = save_run(
        tmp_path, booster, _metadata(), {"features": ["a"]}, {}, suffix="narrow"
    )

    assert (tmp_path / "latest").resolve() == directory.resolve()


def test_an_unsuffixed_save_publishes_no_extra_symlink(tmp_path, booster):
    save_run(tmp_path, booster, _metadata(), {"features": ["a"]}, {})

    assert not list(tmp_path.glob("latest-*"))


def test_save_run_rejects_an_unsafe_suffix(tmp_path, booster):
    metadata = _metadata()

    with pytest.raises(ValueError):
        save_run(tmp_path, booster, metadata, {"features": ["a"]}, {}, suffix="../x")


def test_git_sha_prefers_the_env_override(monkeypatch):
    """The training container has no .git, so the sha is passed in (GH-57)."""
    monkeypatch.setenv("AURUM_GIT_SHA", "0123456789abcdef")

    assert git_sha() == "0123456789abcdef"
    assert git_sha(short=True) == "0123456"


def test_git_sha_falls_back_to_git_when_the_override_is_empty(monkeypatch):
    monkeypatch.setenv("AURUM_GIT_SHA", "")

    # Either a real sha from this repo or "unknown" outside one — never the empty string.
    assert git_sha(short=True)


def test_save_run_writes_every_artifact(tmp_path, booster):
    directory = save_run(tmp_path, booster, _metadata(), {"features": ["a"]}, {"rows_in": 1})

    for name in (
        "model.txt",
        "metadata.json",
        "feature_manifest.json",
        "preprocess_manifest.json",
    ):
        assert (directory / name).exists(), name
    assert json.loads((directory / "metadata.json").read_text())["git_sha"] == "abc1234"


def test_latest_resolves_to_the_saved_run(tmp_path, booster):
    directory = save_run(tmp_path, booster, _metadata(), {"features": ["a"]}, {})

    assert (tmp_path / "latest").resolve() == directory.resolve()


def test_a_second_save_repoints_latest(tmp_path, booster):
    save_run(tmp_path, booster, _metadata("20260905-aaaaaaa"), {"features": ["a"]}, {})
    newer = save_run(tmp_path, booster, _metadata("20260906-bbbbbbb"), {"features": ["a"]}, {})

    assert (tmp_path / "latest").resolve() == newer.resolve()
    # The superseded run is still on disk — repointing is not deleting.
    assert (tmp_path / "20260905-aaaaaaa").exists()
    assert not list(tmp_path.glob(".latest.*"))


def test_load_run_round_trips_predictions(tmp_path, booster):
    """Reloading through the registry reproduces predictions exactly."""
    save_run(tmp_path, booster, _metadata(), {"features": ["a", "b", "c"]}, {})
    reloaded, manifest = load_run(tmp_path)

    data = np.random.default_rng(1).normal(size=(20, 3))
    np.testing.assert_array_equal(booster.predict(data), reloaded.predict(data))
    assert manifest["features"] == ["a", "b", "c"]


def test_config_hash_is_stable_and_sensitive():
    config, same = _config(), _config()
    assert config_hash(config) == config_hash(same)

    changed = _config()
    changed.target = "fwd_ret_21d"
    assert config_hash(changed) != config_hash(config)


def test_file_hash_returns_none_for_a_missing_file(tmp_path):
    assert file_hash(tmp_path / "nope.json") is None

    path = tmp_path / "manifest.json"
    path.write_text("{}")
    assert len(file_hash(path)) == 64


def test_package_versions_records_what_produced_the_model():
    versions = package_versions()

    assert "lightgbm" in versions
    assert versions["lightgbm"]


def test_metadata_proves_the_holdout_was_untouched(tmp_path, booster):
    """The acceptance criterion, checked the way it will be checked on a real run:
    every fold's validation window ends before the holdout boundary."""
    metadata = {
        "version": "20260905-abc1234",
        "holdout_starts_at_fold": 298,
        "folds": [
            {"valid_end_fold": 132, "valid_end_date": "2011-01-31"},
            {"valid_end_fold": 297, "valid_end_date": "2024-09-30"},
        ],
    }
    directory = save_run(tmp_path, booster, metadata, {"features": []}, {})

    saved = json.loads((directory / "metadata.json").read_text())
    assert all(
        fold["valid_end_fold"] < saved["holdout_starts_at_fold"] for fold in saved["folds"]
    )
