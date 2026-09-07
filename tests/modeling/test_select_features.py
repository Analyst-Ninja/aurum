"""`run_select_features` end to end, plus the SHAP pieces the existing unit tests skip.

Contributions come from `booster.predict(pred_contrib=True)` rather than the `shap`
package, and stability is measured across era blocks of the one shipped booster rather
than across refits that no longer exist — both substitutions have to be visible in the
artifacts, not only in the docstring.
"""

import numpy as np
import pandas as pd
import pytest

from src.modeling.explain import shap_report
from src.modeling.models.registry import load_run
from tests.modeling.conftest import prepared


@pytest.fixture
def booster_and_sample(config, panel, saved_run):
    booster, manifest = load_run(config.output_dir, saved_run)
    frame, matrix, _ = prepared(panel, config)
    return booster, matrix.head(120), frame["date"].head(120)


def test_block_contributions_drop_the_bias_column(booster_and_sample):
    """`pred_contrib` appends the base value, which is not an attribution."""
    booster, matrix, _ = booster_and_sample

    means = shap_report.block_contributions(booster, matrix)

    assert means.shape == (matrix.shape[1],)
    assert (means >= 0).all()


def test_the_ranking_has_one_row_per_feature_sorted_by_importance(booster_and_sample):
    booster, matrix, dates = booster_and_sample

    ranking, _ = shap_report.shap_ranking(booster, matrix, dates, 2)

    assert sorted(ranking["feature_name"]) == sorted(matrix.columns)
    assert ranking["mean_abs_shap"].is_monotonic_decreasing


def test_the_per_block_detail_names_its_blocks_by_date_range(booster_and_sample):
    """The block detail is what makes the era-vs-refit substitution visible."""
    booster, matrix, dates = booster_and_sample

    _, per_block = shap_report.shap_ranking(booster, matrix, dates, 2)

    assert sorted(per_block["block_index"].unique()) == [0, 1]
    assert per_block["block_start"].min() <= per_block["block_end"].max()
    assert len(per_block) == 2 * matrix.shape[1]


def test_the_std_is_zero_when_every_block_agrees(booster_and_sample):
    booster, matrix, dates = booster_and_sample

    ranking, _ = shap_report.shap_ranking(booster, matrix, dates, 1)

    assert (ranking["std_abs_shap"] == 0).all()


def test_era_blocks_never_cut_a_cross_section_in_half():
    dates = pd.Series(pd.to_datetime(["2024-01-01"] * 3 + ["2024-01-02"] * 3))

    blocks = shap_report.era_blocks(dates, 2)

    assert [len(block) for block in blocks] == [3, 3]


def test_more_blocks_than_dates_cannot_produce_an_empty_block():
    dates = pd.Series(pd.to_datetime(["2024-01-01", "2024-01-02"]))

    blocks = shap_report.era_blocks(dates, 10)

    assert len(blocks) == 2


def test_no_dates_means_no_blocks():
    assert shap_report.era_blocks(pd.Series(dtype="datetime64[ns]"), 3) == []


@pytest.fixture
def selected(config, panel, saved_run, loaded, tmp_path, monkeypatch):
    """Run the real selection over the miniature panel."""
    config_path = tmp_path / "run.yaml"
    config_path.write_text("source: {}\n")
    monkeypatch.setattr(shap_report, "load_config", lambda path: config)

    ranking_path = shap_report.run_select_features(str(config_path), saved_run)
    return pd.read_csv(ranking_path), ranking_path, config_path


def test_the_ranking_csv_carries_the_documented_schema(selected):
    ranking, _, _ = selected

    assert list(ranking.columns) == [
        "feature_name",
        "rank",
        "mean_abs_shap",
        "std_abs_shap",
        "cluster_id",
        "cutoff_rule",
        "selected",
    ]


def test_the_block_detail_is_written_beside_the_ranking(selected):
    _, path, _ = selected

    assert (path.parent / shap_report.PER_FOLD).exists()


def test_the_cap_binds_before_the_cumulative_share_on_a_flat_profile(selected, config):
    ranking, _, _ = selected

    assert int(ranking["selected"].sum()) <= config.select.max_features


def test_the_seed_is_rewritten_with_every_feature_ranked(selected, config):
    ranking, _, _ = selected

    seed = pd.read_csv(config.select.seed_path)
    assert list(seed.columns) == [
        "feature_name",
        "rank",
        "mean_abs_shap",
        "selected",
        "model_version",
    ]
    assert len(seed) == len(ranking)
    # Written as the lowercase literals dbt's seed loader expects, not Python bools.
    raw = pd.read_csv(config.select.seed_path, dtype=str)
    assert set(raw["selected"]) <= {"true", "false"}


def test_the_seed_records_the_run_it_came_from(selected, config, saved_run):
    seed = pd.read_csv(config.select.seed_path)

    assert seed["model_version"].unique().tolist() == [saved_run]


def test_the_narrow_config_carries_only_the_selected_features(selected):
    import yaml

    ranking, _, config_path = selected
    narrow = config_path.with_name(f"{config_path.stem}_narrow{config_path.suffix}")

    allow_list = yaml.safe_load(narrow.read_text())["preprocess"]["allow_list"]
    assert allow_list == ranking.loc[ranking["selected"], "feature_name"].tolist()


def test_latest_is_resolved_to_the_directory_it_points_at(config, saved_run):
    """The seed records a real version id, never the word `latest`."""
    assert shap_report._version_of(config, "latest") == saved_run


def test_a_config_with_no_evaluation_rows_refuses_to_explain(
    config, panel, saved_run, loaded, tmp_path, monkeypatch
):
    config.splits.burn_in_folds = 999
    monkeypatch.setattr(shap_report, "load_config", lambda path: config)
    config_path = tmp_path / "run.yaml"
    config_path.write_text("source: {}\n")

    with pytest.raises(ValueError, match="nothing to explain"):
        shap_report.run_select_features(str(config_path), saved_run)


def test_the_sample_is_drawn_per_date_not_uniformly(config, panel):
    """A uniform draw would quietly weight the ranking toward the busier recent years."""
    frame, _, _ = prepared(panel, config)
    picked = shap_report.stratified_sample(frame["date"], 100, 42)

    per_date = frame["date"].iloc[picked].value_counts()
    assert per_date.nunique() == 1
    assert np.all(np.diff(picked) > 0)
