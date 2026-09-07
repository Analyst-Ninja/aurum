"""The config-path guard.

Both CLIs open a file named by `-c`, so the argument is untrusted whenever the caller
is a scheduler, a container command or an agent. `safe_path` is the one place that
decides whether a path is allowed to be opened at all.
"""

import pytest

from src.utils.paths import PROJECT_ROOT, safe_path


def test_a_config_inside_the_project_resolves():
    inside = PROJECT_ROOT / "src" / "ingestion" / "configs"
    assert safe_path(inside) == inside.resolve()


def test_a_temp_path_resolves(tmp_path):
    """pytest fixtures and the container's scratch dir both live under the temp root."""
    target = tmp_path / "run.yaml"
    target.write_text("a: 1\n")

    assert safe_path(target) == target.resolve()


def test_a_traversal_out_of_the_project_is_refused():
    with pytest.raises(ValueError, match="outside the project root"):
        safe_path(PROJECT_ROOT / ".." / ".." / ".." / "etc" / "passwd")


def test_an_absolute_path_elsewhere_is_refused():
    with pytest.raises(ValueError, match="outside the project root"):
        safe_path("/etc/hosts")
