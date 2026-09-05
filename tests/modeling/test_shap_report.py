"""Selection maths and the two guards on the seed."""

import numpy as np
import pandas as pd
import pytest

from src.modeling.explain.seed_writer import write_narrow_config, write_seed
from src.modeling.explain.shap_report import (
    apply_cutoff,
    cluster_by_correlation,
    era_blocks,
    stratified_sample,
)


def _ranking(values: list[float], clusters: list[int] | None = None) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "feature_name": [f"f{i}" for i in range(len(values))],
            "mean_abs_shap": values,
            "std_abs_shap": [0.0] * len(values),
            "cluster_id": clusters or list(range(1, len(values) + 1)),
        }
    )


def test_cutoff_is_bound_by_the_cap_on_a_flat_profile():
    ranking, rule = apply_cutoff(_ranking([1.0] * 150), cum_share=0.95, max_features=40)
    assert rule == "cap"
    assert int(ranking["selected"].sum()) == 40


def test_cutoff_is_bound_by_the_cumulative_share_when_importance_concentrates():
    values = [10.0, 8.0, 6.0] + [0.01] * 100
    ranking, rule = apply_cutoff(_ranking(values), cum_share=0.95, max_features=40)
    assert rule == "cumulative"
    assert int(ranking["selected"].sum()) < 10


def test_only_the_strongest_member_of_a_cluster_is_selected():
    ranking = _ranking([5.0, 4.0, 3.0], clusters=[1, 1, 2])
    selected, _ = apply_cutoff(ranking, cum_share=0.95, max_features=40)
    chosen = set(selected.loc[selected["selected"], "feature_name"])
    assert "f0" in chosen and "f1" not in chosen


def test_perfectly_correlated_features_share_a_cluster():
    rng = np.random.default_rng(0)
    base = rng.normal(size=500)
    matrix = pd.DataFrame(
        {"a": base, "a_z": base * 3.0 + 1.0, "b": rng.normal(size=500)}
    )
    ranking = pd.DataFrame({"feature_name": ["a", "a_z", "b"]})

    clusters = cluster_by_correlation(matrix, ranking, threshold=0.95)
    assert clusters["a"] == clusters["a_z"]
    assert clusters["b"] != clusters["a"]


def test_sample_draws_the_same_quota_from_every_date():
    dates = pd.Series(pd.to_datetime(["2020-01-01"] * 100 + ["2020-01-02"] * 400))
    picked = stratified_sample(dates, n_rows=100, seed=7)
    counts = dates.iloc[picked].value_counts()
    assert counts.nunique() == 1
    assert (np.diff(picked) > 0).all()


def test_era_blocks_split_on_dates_not_rows():
    dates = pd.Series(pd.to_datetime(["2020-01-01"] * 3 + ["2020-01-02"] * 3))
    blocks = era_blocks(dates, n_blocks=2)
    assert [len(block) for block in blocks] == [3, 3]


def test_write_seed_rejects_a_target_column(tmp_path):
    ranking = _ranking([1.0, 0.5])
    ranking.loc[0, "feature_name"] = "fwd_ret_5d"
    ranking, _ = apply_cutoff(ranking, cum_share=0.95, max_features=40)

    with pytest.raises(ValueError, match="Target columns"):
        write_seed(ranking, "v1", tmp_path / "selected_features.csv")


def test_write_seed_rejects_an_empty_selection(tmp_path):
    ranking, _ = apply_cutoff(_ranking([1.0, 0.5]), cum_share=0.95, max_features=40)
    ranking["selected"] = False

    with pytest.raises(ValueError, match="zero selected"):
        write_seed(ranking, "v1", tmp_path / "selected_features.csv")


def test_write_seed_keeps_every_feature_and_the_schema(tmp_path):
    ranking, _ = apply_cutoff(_ranking([5.0, 0.01, 0.005]), cum_share=0.95, max_features=40)
    path = write_seed(ranking, "20260906-abc1234", tmp_path / "selected_features.csv")

    seed = pd.read_csv(path, dtype=str)
    assert list(seed.columns) == [
        "feature_name",
        "rank",
        "mean_abs_shap",
        "selected",
        "model_version",
    ]
    assert len(seed) == 3
    assert seed["selected"].isin(["true", "false"]).all()
    assert (seed["model_version"] == "20260906-abc1234").all()


def test_narrow_config_carries_the_selected_features(tmp_path):
    base = tmp_path / "run.yaml"
    base.write_text("target: fwd_ret_5d_excess\npreprocess:\n  warmup_bars: 252\n")
    ranking, _ = apply_cutoff(_ranking([5.0, 0.01]), cum_share=0.95, max_features=40)

    import yaml

    payload = yaml.safe_load(write_narrow_config(base, ranking).read_text())
    assert payload["preprocess"]["allow_list"] == ["f0"]
    assert payload["preprocess"]["warmup_bars"] == 252
