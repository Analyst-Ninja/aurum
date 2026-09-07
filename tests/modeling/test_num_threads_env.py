"""AURUM_NUM_THREADS: the ECS train task's vCPU count, injected.

ModelParams.num_threads defaults to 4, tuned for an Apple Silicon laptop where the
efficiency cores drag every boosting barrier. That reasoning does not hold on Fargate,
where vCPUs are homogeneous — a task sized at 8 vCPU must use 8 or it pays for half an
idle box for the whole fit.
"""

import pytest

from src.modeling.config import ModelParams, _default_num_threads


def test_defaults_to_four_when_unset(monkeypatch):
    monkeypatch.delenv("AURUM_NUM_THREADS", raising=False)

    assert _default_num_threads() == 4
    assert ModelParams().num_threads == 4


def test_env_var_overrides_the_laptop_default(monkeypatch):
    monkeypatch.setenv("AURUM_NUM_THREADS", "8")

    assert ModelParams().num_threads == 8


def test_an_explicit_config_value_still_wins(monkeypatch):
    """The YAML is the most specific source and must beat the environment."""
    monkeypatch.setenv("AURUM_NUM_THREADS", "8")

    assert ModelParams(num_threads=2).num_threads == 2


@pytest.mark.parametrize("bad", ["eight", "", " "])
def test_a_non_integer_is_rejected_or_ignored(monkeypatch, bad):
    monkeypatch.setenv("AURUM_NUM_THREADS", bad)

    if bad.strip():
        with pytest.raises(ValueError, match="AURUM_NUM_THREADS"):
            _default_num_threads()
    else:
        assert _default_num_threads() == 4


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_a_thread_count_below_one_is_rejected(monkeypatch, bad):
    monkeypatch.setenv("AURUM_NUM_THREADS", bad)

    with pytest.raises(ValueError, match="must be >= 1"):
        _default_num_threads()
