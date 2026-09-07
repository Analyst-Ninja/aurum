"""`evaluate` — the harness that writes `metrics.json`.

The split it enforces is the point: every headline number comes from the holdout,
scored once with the shipped booster, and the fold numbers are labelled
`model_selection` so nobody quotes them as out-of-sample.
"""

import json

import numpy as np
import pandas as pd
import pytest

from src.modeling.evaluate import runner
from tests.modeling.conftest import prepared


@pytest.fixture
def metrics(config, panel, saved_run, loaded, tmp_path, monkeypatch):
    """Run the real harness end to end and hand back the parsed report."""
    monkeypatch.setattr(runner, "load_config", lambda path: config)
    path = runner.evaluate("ignored.yaml", saved_run)
    return json.loads(path.read_text()), path


def test_metrics_json_lands_beside_the_run(metrics, config, saved_run):
    _, path = metrics

    assert path == (config.output_dir / saved_run).resolve() / runner.METRICS


def test_the_report_identifies_the_run_it_scored(metrics, saved_run):
    report, _ = metrics

    assert report["version"] == saved_run
    assert report["git_sha"] == "testsha"
    assert report["target"] == "fwd_ret_5d_excess"
    assert report["horizon"] == 5


def test_the_headline_block_is_the_holdout_and_says_so(metrics, config):
    report, _ = metrics

    assert report["holdout"]["purpose"] == "headline"
    assert report["holdout"]["starts_at_fold"] == config.splits.eval_end_fold + 1


def test_the_holdout_window_is_reported_as_dates(metrics):
    report, _ = metrics

    assert report["holdout"]["start_date"] < report["holdout"]["end_date"]


def test_every_headline_metric_is_present(metrics):
    model = metrics[0]["holdout"]["model"]

    assert {"ic", "icir", "decile_spread", "long_short_sharpe", "turnover"} <= set(model)


def test_all_four_baselines_are_scored_on_the_same_rows(metrics):
    baselines = metrics[0]["holdout"]["baselines"]

    assert set(baselines) == {
        "zero",
        "momentum_mom_12_1_z",
        "reversal_5d_z",
        "ridge_z_features",
    }
    assert {b["n_rows"] for b in baselines.values()} == {
        metrics[0]["holdout"]["model"]["n_rows"]
    }


def test_the_ridge_baseline_records_how_many_z_columns_it_saw(metrics):
    ridge = metrics[0]["holdout"]["baselines"]["ridge_z_features"]

    assert ridge["n_features"] > 0
    assert "0.0" in ridge["nan_policy"]


def test_the_breakdowns_cut_the_holdout_by_sector_and_by_regime(metrics):
    breakdowns = metrics[0]["holdout"]["breakdowns"]

    assert set(breakdowns) == {"sector", "volatility_regime"}
    assert set(breakdowns["sector"]) <= {"Technology", "Energy", "Health Care"}
    assert set(breakdowns["volatility_regime"]) <= set(runner.REGIME_LABELS)


def test_the_secondary_heads_are_fitted_once_and_record_the_deviation(metrics):
    heads = metrics[0]["holdout"]["secondary_heads"]

    assert "refitted per fold" in heads["deviation"]
    assert "classifier_label_up_5d" in heads
    assert "ranker_fwd_ret_5d_xs_decile" in heads


def test_the_caveats_travel_with_the_numbers(metrics):
    assert metrics[0]["caveats"] == runner.CAVEATS


def test_the_fold_block_is_labelled_model_selection(metrics):
    folds = metrics[0]["folds"]

    assert folds["purpose"] == "model_selection"


def test_without_the_parquet_the_fold_block_degrades_to_metadata(metrics):
    """A run trained before #54 has no fold predictions; that must not block a report."""
    folds = metrics[0]["folds"]

    assert folds["source"] == "metadata.json"
    assert [f["fold_index"] for f in folds["per_fold"]] == [0, 1]
    assert folds["per_fold"][0]["mean_ic"] == 0.02


def test_with_the_parquet_the_fold_block_carries_real_metrics(
    config, panel, saved_run, fold_predictions, loaded, monkeypatch
):
    monkeypatch.setattr(runner, "load_config", lambda path: config)

    report = json.loads(runner.evaluate("ignored.yaml", saved_run).read_text())
    folds = report["folds"]

    assert folds["source"] == runner.FOLD_PREDICTIONS
    assert "pooled" in folds
    assert {"ic", "decile_spread"} <= set(folds["per_fold"][0])


def test_a_config_with_no_holdout_rows_refuses_to_report(
    config, panel, saved_run, loaded, monkeypatch
):
    config.splits.eval_end_fold = 999
    monkeypatch.setattr(runner, "load_config", lambda path: config)

    with pytest.raises(ValueError, match="nothing to evaluate"):
        runner.evaluate("ignored.yaml", saved_run)


def test_the_panel_is_rebuilt_without_transforming_the_target(config, panel, saved_run, loaded):
    """A decile spread has to read in returns, not in multiples of the day's dispersion."""
    run_dir = (config.output_dir / saved_run).resolve()
    manifest = json.loads((run_dir / "feature_manifest.json").read_text())
    frame, _ = runner._panel(config, manifest)

    expected, _, _ = prepared(panel, config)
    pd.testing.assert_series_equal(
        frame[config.target].reset_index(drop=True),
        expected[config.target].reset_index(drop=True),
    )


def test_a_block_scores_the_rows_it_is_handed(panel):
    frame = panel.head(200)
    block = runner._block(frame, np.zeros(len(frame)), "fwd_ret_5d_excess", 5)

    assert block["n_rows"] == 200
