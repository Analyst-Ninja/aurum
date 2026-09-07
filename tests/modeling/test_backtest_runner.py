"""`run_backtest` — the artifact set, and which block the quoted number comes from.

The holdout is the result. The evaluation folds are shown beside it because they chose
the hyperparameters, and they are titled so nobody quotes them; the three reality checks
run on the holdout only.
"""

import json

import pandas as pd
import pytest

from src.modeling.backtest import report, runner
from src.modeling.evaluate.runner import FOLD_PREDICTIONS


@pytest.fixture
def backtested(config, panel, saved_run, loaded, monkeypatch):
    monkeypatch.setattr(runner, "load_config", lambda path: config)
    summary_path = runner.run_backtest("ignored.yaml", saved_run)
    return json.loads(summary_path.read_text()), summary_path.parent


def test_every_artifact_is_written(backtested):
    _, out = backtested

    for name in (runner.SUMMARY, runner.YEARLY, runner.EQUITY, runner.POSITIONS,
                 report.TEARSHEET, report.REPORT):
        assert (out / name).exists(), name


def test_the_summary_names_the_run_and_the_construction(backtested, saved_run):
    summary, _ = backtested

    assert summary["version"] == saved_run
    assert summary["horizon"] == 5
    assert summary["construction"]["headline_cost_bps"] == runner.HEADLINE_COST_BPS
    assert summary["construction"]["tranches"] == 5


def test_the_holdout_block_is_titled_as_the_quoted_result(backtested):
    summary, _ = backtested

    assert any("quoted result" in title for title in summary["blocks"])


def test_without_fold_predictions_only_the_holdout_is_simulated(backtested):
    summary, _ = backtested

    assert len(summary["blocks"]) == 1


def test_with_fold_predictions_the_selection_block_is_added_and_labelled(
    config, panel, saved_run, fold_predictions, loaded, monkeypatch
):
    monkeypatch.setattr(runner, "load_config", lambda path: config)

    summary = json.loads(runner.run_backtest("ignored.yaml", saved_run).read_text())

    assert len(summary["blocks"]) == 2
    assert any("not a result" in title for title in summary["blocks"])


def test_every_swept_cost_appears_in_the_block(backtested, config):
    summary, _ = backtested
    block = next(iter(summary["blocks"].values()))

    assert set(block["by_cost_bps"]) == {
        f"{int(cost)}bps" for cost in config.backtest.cost_bps_grid
    }
    assert "break_even_bps" in block


def test_all_three_reality_checks_run_on_the_holdout(backtested):
    summary, _ = backtested

    assert [row["check"] for row in summary["reality_checks"]] == [
        "Randomization",
        "Signal lag (1 day)",
        "Deflated Sharpe",
    ]
    assert set(summary["reality_checks_detail"]) == {
        "randomization",
        "signal_lag",
        "deflated_sharpe",
    }


def test_the_null_distribution_is_not_written_into_the_summary(backtested):
    """500 shuffles of floats would dwarf the file it is a footnote in."""
    summary, _ = backtested

    assert "null_sharpes" not in summary["reality_checks_detail"]["randomization"]


def test_the_known_biases_travel_with_the_result(backtested):
    summary, _ = backtested

    assert summary["assumptions"] == report.BIASES


def test_the_equity_csv_carries_one_column_per_cost_level(backtested, config):
    _, out = backtested

    curves = pd.read_csv(out / runner.EQUITY, index_col=0)
    assert list(curves.columns) == [
        f"{int(cost)} bps" for cost in config.backtest.cost_bps_grid
    ]


def test_positions_are_dollar_neutral_per_formation_date(backtested):
    _, out = backtested

    positions = pd.read_parquet(out / runner.POSITIONS)
    sums = positions.groupby("date")["weight"].sum()
    assert sums.abs().max() < 1e-9


def test_net_sharpe_at_reads_the_headline_cost_back(backtested):
    summary, out = backtested

    expected = next(iter(summary["blocks"].values()))["by_cost_bps"]["10bps"]["sharpe"]
    assert runner.net_sharpe_at(out / runner.SUMMARY) == pytest.approx(expected)


def test_net_sharpe_at_can_be_asked_for_another_cost_level(backtested):
    summary, out = backtested

    expected = next(iter(summary["blocks"].values()))["by_cost_bps"]["0bps"]["sharpe"]
    assert runner.net_sharpe_at(out / runner.SUMMARY, 0.0) == pytest.approx(expected)


def test_a_run_with_no_holdout_rows_refuses_to_simulate(
    config, panel, saved_run, loaded, monkeypatch
):
    config.splits.eval_end_fold = 999
    monkeypatch.setattr(runner, "load_config", lambda path: config)

    with pytest.raises(ValueError, match="No rows past fold 999"):
        runner.run_backtest("ignored.yaml", saved_run)


def test_a_block_that_forms_no_legs_names_itself_in_the_error(config):
    """One symbol per date cannot fill both decile legs, and the message must say which."""
    predictions = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-01", periods=20),
            "symbol": "AAA",
            "y": 0.01,
            "pred": 0.5,
        }
    )

    with pytest.raises(ValueError, match="Holdout: no date formed both decile legs"):
        runner._block("Holdout", "", predictions, config)


def test_a_missing_fold_parquet_is_logged_not_raised(
    config, panel, saved_run, loaded, monkeypatch, caplog
):
    monkeypatch.setattr(runner, "load_config", lambda path: config)

    with caplog.at_level("WARNING"):
        runner.run_backtest("ignored.yaml", saved_run)

    assert FOLD_PREDICTIONS in caplog.text
