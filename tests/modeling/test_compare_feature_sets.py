"""#55's gate: does the narrowed feature set beat the full one on the holdout?

Two-sided on purpose. Feature selection is a hypothesis, not an improvement — a
narrowed model that loses on either ICIR or decile spread does not get its seed
committed, and the verdict says so in words a human reads before running `dbt seed`.
"""

import json

import pytest

from src.modeling.explain.seed_writer import COMPARISON, compare_feature_sets


def _metrics(path, ic=0.02, icir=0.5, spread=0.004):
    path.write_text(
        json.dumps(
            {
                "holdout": {
                    "model": {
                        "ic": {"mean": ic},
                        "icir": icir,
                        "decile_spread": {"mean": spread},
                    }
                }
            }
        )
    )
    return path


@pytest.fixture
def full(tmp_path):
    return _metrics(tmp_path / "full.json")


@pytest.fixture
def narrow(tmp_path):
    return tmp_path / "narrow" / "metrics.json"


@pytest.fixture(autouse=True)
def narrow_dir(narrow):
    narrow.parent.mkdir(parents=True, exist_ok=True)


def test_matching_both_numbers_wins(full, narrow):
    report = compare_feature_sets(full, _metrics(narrow))

    assert report["narrowed_wins"] is True
    assert report["verdict"] == "Commit the seed."


def test_beating_both_numbers_wins(full, narrow):
    report = compare_feature_sets(full, _metrics(narrow, icir=0.9, spread=0.006))

    assert report["narrowed_wins"] is True


def test_a_better_icir_does_not_excuse_a_worse_spread(full, narrow):
    report = compare_feature_sets(full, _metrics(narrow, icir=0.9, spread=0.001))

    assert report["narrowed_wins"] is False
    assert "Do not commit the seed" in report["verdict"]


def test_a_better_spread_does_not_excuse_a_worse_icir(full, narrow):
    report = compare_feature_sets(full, _metrics(narrow, icir=0.1, spread=0.009))

    assert report["narrowed_wins"] is False


def test_both_headline_blocks_are_carried_with_their_paths(full, narrow):
    report = compare_feature_sets(full, _metrics(narrow, ic=0.03))

    assert report["full"]["metrics_path"] == str(full)
    assert report["narrowed"]["ic"] == 0.03
    assert set(report["full"]) == {"metrics_path", "ic", "icir", "decile_spread"}


def test_the_comparison_lands_beside_the_narrowed_run(full, narrow):
    compare_feature_sets(full, _metrics(narrow))

    written = json.loads((narrow.parent / COMPARISON).read_text())
    assert written["narrowed_wins"] is True
