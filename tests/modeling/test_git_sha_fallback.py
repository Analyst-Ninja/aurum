"""`git_sha` outside a repository.

The training image ships without git and without `.git`, so this path is the one the
container actually takes when `AURUM_GIT_SHA` is not passed through — and a run stamped
`unknown` shares its version id with every other run that day.
"""

import subprocess

from src.modeling.models import registry


def test_a_missing_git_binary_stamps_the_run_unknown(monkeypatch):
    monkeypatch.delenv("AURUM_GIT_SHA", raising=False)
    monkeypatch.setattr(
        registry.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
    )

    assert registry.git_sha() == "unknown"


def test_a_directory_that_is_not_a_repository_stamps_the_run_unknown(monkeypatch):
    monkeypatch.delenv("AURUM_GIT_SHA", raising=False)

    def _fail(*args, **kwargs):
        raise subprocess.CalledProcessError(128, args[0])

    monkeypatch.setattr(registry.subprocess, "run", _fail)

    assert registry.git_sha(short=True) == "unknown"


def test_the_version_id_still_forms_without_a_sha(monkeypatch):
    monkeypatch.delenv("AURUM_GIT_SHA", raising=False)
    monkeypatch.setattr(
        registry.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
    )

    assert registry.version_id().endswith("-unknown")
