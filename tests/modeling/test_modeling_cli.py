"""The modelling CLI: argument wiring, and the two subcommands it implements itself.

Every other subcommand is a one-line delegation, and the assertions on those are that
the right function is reached with the right arguments — the functions themselves are
tested against real data in their own modules.
"""

import json

import numpy as np
import pandas as pd
import pytest

from src.modeling import cli


@pytest.fixture
def argv(monkeypatch):
    def _set(*args):
        monkeypatch.setattr("sys.argv", ["aurum-model", *args])

    return _set


@pytest.fixture
def dispatched(monkeypatch):
    """Replace every delegation target and record which one main() reached."""
    calls = {}

    for name in ("train", "predict", "compare", "export"):
        monkeypatch.setattr(cli, name, lambda args, name=name: calls.setdefault(name, args))
    monkeypatch.setattr(
        cli, "run_evaluate", lambda config, version: calls.setdefault("evaluate", (config, version))
    )
    monkeypatch.setattr(
        cli,
        "run_select_features",
        lambda config, version: calls.setdefault("select-features", (config, version)),
    )
    monkeypatch.setattr(
        cli,
        "run_backtest",
        lambda config, version: calls.setdefault("backtest", (config, version)),
    )
    return calls


# ------------------------------------------------------------------------ dispatch


def test_train_is_reached_with_its_config(argv, dispatched):
    argv("train", "-c", "run.yaml")

    cli.main()

    assert dispatched["train"].config == "run.yaml"


def test_train_takes_a_version_suffix_and_defaults_it_to_none(argv, dispatched):
    """Without it the weekly full and narrow runs share a version id."""
    argv("train", "-c", "run.yaml")
    cli.main()
    assert dispatched["train"].version_suffix is None

    dispatched.clear()
    argv("train", "-c", "run.yaml", "--version-suffix", "narrow")
    cli.main()
    assert dispatched["train"].version_suffix == "narrow"


def test_train_takes_no_version_because_it_creates_one(argv, dispatched):
    argv("train", "-c", "run.yaml")

    cli.main()

    assert not hasattr(dispatched["train"], "version")


@pytest.mark.parametrize("command", ["evaluate", "select-features", "backtest"])
def test_a_scoring_subcommand_forwards_the_config_and_version(argv, dispatched, command):
    argv(command, "-c", "run.yaml", "--version", "20260907-abc")

    cli.main()

    assert dispatched[command] == ("run.yaml", "20260907-abc")


@pytest.mark.parametrize("command", cli.VERSIONED)
def test_every_versioned_subcommand_defaults_to_latest(argv, monkeypatch, command):
    seen = {}
    monkeypatch.setattr(cli, "train", lambda args: None)
    for name in ("predict", "compare", "export"):
        monkeypatch.setattr(cli, name, lambda args: seen.setdefault("version", args.version))
    for name in ("run_evaluate", "run_select_features", "run_backtest"):
        monkeypatch.setattr(
            cli, name, lambda config, version: seen.setdefault("version", version)
        )
    extra = ["--baseline", "20260907-full"] if command == "compare" else []
    argv(command, "-c", "run.yaml", *extra)

    cli.main()

    assert seen["version"] == "latest"


def test_predict_takes_an_asof_date(argv, dispatched):
    argv("predict", "-c", "run.yaml", "--asof", "2026-09-05")

    cli.main()

    assert dispatched["predict"].asof == "2026-09-05"


def test_compare_requires_a_baseline_to_compare_against(argv, dispatched, capsys):
    argv("compare", "-c", "run.yaml", "--version", "narrow")

    with pytest.raises(SystemExit):
        cli.main()

    assert "--baseline" in capsys.readouterr().err


def test_a_subcommand_is_required(argv, capsys):
    argv()

    with pytest.raises(SystemExit):
        cli.main()


def test_a_config_is_required(argv, capsys):
    argv("train")

    with pytest.raises(SystemExit):
        cli.main()

    assert "--config" in capsys.readouterr().err


def test_an_unknown_subcommand_is_rejected_by_the_parser(argv):
    argv("frobnicate", "-c", "run.yaml")

    with pytest.raises(SystemExit):
        cli.main()


def test_a_pending_subcommand_names_the_issue_that_lands_it(argv, monkeypatch):
    """`PENDING` is empty today; the branch is what keeps the interface honest."""
    monkeypatch.setattr(cli, "PENDING", {"tune": "GH-99"})

    argv("tune", "-c", "run.yaml")

    with pytest.raises(NotImplementedError, match="GH-99"):
        cli.main()


# ------------------------------------------------------------------------ train


def test_train_writes_a_run_directory_and_its_fold_predictions(
    config, panel, loaded, monkeypatch, tmp_path
):
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "load_training_frame", lambda cfg: panel.copy())
    args = type("Args", (), {"config": "run.yaml", "version_suffix": None})()

    cli.train(args)

    directory = (config.output_dir / "latest").resolve()
    assert (directory / "model.txt").exists()
    assert (directory / "fold_predictions.parquet").exists()


def test_the_metadata_records_what_would_deflate_the_sharpe_downstream(
    config, panel, loaded, monkeypatch
):
    """`n_configs_tried` is read by the deflated-Sharpe check; a widened grid must show."""
    config.train.grid = list(config.train.grid) * 2
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "load_training_frame", lambda cfg: panel.copy())

    cli.train(type("Args", (), {"config": "run.yaml", "version_suffix": None})())

    metadata = json.loads(
        ((config.output_dir / "latest").resolve() / "metadata.json").read_text()
    )
    assert metadata["n_configs_tried"] == 2
    assert metadata["holdout_starts_at_fold"] == config.splits.eval_end_fold + 1
    assert metadata["n_features"] > 0
    assert len(metadata["folds"]) > 0


def test_a_suffix_publishes_its_own_stable_handle(config, panel, loaded, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "load_training_frame", lambda cfg: panel.copy())

    cli.train(type("Args", (), {"config": "run.yaml", "version_suffix": "narrow"})())

    assert (config.output_dir / "latest-narrow").is_symlink()


def test_the_fold_predictions_hold_the_raw_return_not_the_standardized_target(
    config, panel, loaded, monkeypatch
):
    """A decile spread has to read in returns, not in multiples of the day's dispersion."""
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "load_training_frame", lambda cfg: panel.copy())

    cli.train(type("Args", (), {"config": "run.yaml", "version_suffix": None})())

    predictions = pd.read_parquet(
        (config.output_dir / "latest").resolve() / "fold_predictions.parquet"
    )
    assert predictions["y"].abs().max() < 1.0  # a return, not a z-score
    assert set(predictions.columns) == {"fold_index", "symbol", "date", "y", "pred"}


def test_prepare_records_the_filters_it_applied(config, panel, loaded, monkeypatch):
    monkeypatch.setattr(cli, "load_training_frame", lambda cfg: panel.copy())

    _, matrix, features, preprocess, raw_target = cli._prepare(config)

    assert preprocess["source_table"] == "gold.mart_training_set"
    assert preprocess["rows_in"] == len(panel)
    assert [f["name"] for f in preprocess["filters"]] == [
        "null_target",
        "warmup_burnin",
        "min_cross_section",
    ]
    assert preprocess["target_transforms"] == [
        "winsorize_1_99_per_date",
        "standardize_per_date",
    ]
    assert config.target not in matrix.columns
    assert len(raw_target) == len(matrix)
    assert features["nan_policy"] == "native"


def test_a_fold_whose_prediction_length_disagrees_is_skipped(tmp_path):
    """A fold that early-stopped without predicting cannot be aligned to its rows."""
    folds = [
        type("Fold", (), {"valid_idx": np.array([0, 1])})(),
        type("Fold", (), {"valid_idx": np.array([2, 3])})(),
    ]
    fits = [
        type("Fit", (), {"fold_index": 0, "valid_pred": np.array([0.1, 0.2])})(),
        type("Fit", (), {"fold_index": 1, "valid_pred": np.array([0.3])})(),
    ]
    series = pd.Series(range(4))

    cli._save_fold_predictions(tmp_path, folds, fits, series, series, series.astype(float))

    written = pd.read_parquet(tmp_path / "fold_predictions.parquet")
    assert written["fold_index"].unique().tolist() == [0]


# ------------------------------------------------------------------------ predict


def test_predict_scores_the_latest_date_through_the_stored_manifest(
    config, panel, saved_run, loaded, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "load_training_frame", lambda cfg: panel.copy())

    cli.predict(type("Args", (), {"config": "run.yaml", "version": saved_run, "asof": None})())

    printed = capsys.readouterr().out
    assert "symbol" in printed and "score" in printed


def test_predict_reads_the_feature_mart_not_the_training_set(
    config, panel, saved_run, loaded, monkeypatch, capsys
):
    """`mart_features` has no targets, which is what makes the manifest replay real."""
    seen = {}

    def _load(cfg):
        seen["table"] = cfg.source.table
        return panel.copy()

    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "load_training_frame", _load)

    cli.predict(type("Args", (), {"config": "run.yaml", "version": saved_run, "asof": None})())

    assert seen["table"] == "mart_features"


def test_asof_truncates_the_panel_before_the_latest_date_is_taken(
    config, panel, saved_run, loaded, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "load_training_frame", lambda cfg: panel.copy())
    asof = "2024-02-01"

    cli.predict(type("Args", (), {"config": "run.yaml", "version": saved_run, "asof": asof})())

    printed = capsys.readouterr().out
    assert "2024-02-01" in printed


def test_a_reordered_feature_matrix_raises_rather_than_scoring(
    config, panel, saved_run, loaded, monkeypatch
):
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    shuffled = panel[list(reversed(panel.columns))].copy()
    monkeypatch.setattr(cli, "load_training_frame", lambda cfg: shuffled)

    with pytest.raises(ValueError, match="does not match the manifest"):
        cli.predict(
            type("Args", (), {"config": "run.yaml", "version": saved_run, "asof": None})()
        )


# ------------------------------------------------------------------------ compare / export


def test_compare_reads_two_metrics_files_and_scores_neither(config, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(
        cli, "compare_feature_sets", lambda full, narrow: seen.update(full=full, narrow=narrow)
    )

    cli.compare(
        type("Args", (), {"config": "run.yaml", "version": "narrow", "baseline": "full"})()
    )

    assert seen["full"].name == cli.METRICS
    assert seen["full"].parent.name == "full"
    assert seen["narrow"].parent.name == "narrow"


def test_export_publishes_the_run_under_the_configured_export_settings(config, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "upload_run", lambda root, version, settings: seen.update(
        root=root, version=version, settings=settings
    ))

    cli.export(type("Args", (), {"config": "run.yaml", "version": "latest"})())

    assert seen == {
        "root": config.output_dir,
        "version": "latest",
        "settings": config.export,
    }
