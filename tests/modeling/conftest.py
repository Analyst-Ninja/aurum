"""A miniature of the real panel, plus a saved run over it.

Everything downstream of training — evaluate, select-features, backtest — reads a run
directory and a `mart_training_set`-shaped frame. Both are built here once: a synthetic
panel small enough to fit a booster in under a second, wide enough that the deny-lists,
the sector/regime breakdowns and the secondary heads all have something to bite on.

The signal is deliberately real but weak: `pred` correlates with the target through
`feat_signal`, so IC and decile spread come out positive and a test can assert on their
sign without asserting on a fitted value.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.modeling.config import ModelingConfig
from src.modeling.data.preprocess import add_indicators, build_features, filter_rows

N_DATES = 90
N_SYMBOLS = 24
# Enough rows per date for `deciles` to fill all ten buckets.
SECTORS = ["Technology", "Energy", "Health Care"]


def make_panel(n_dates: int = N_DATES, n_symbols: int = N_SYMBOLS, seed: int = 0) -> pd.DataFrame:
    """A `mart_training_set` lookalike: one row per (date, symbol)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n_dates)
    symbols = [f"SYM{i:02d}" for i in range(n_symbols)]
    index = pd.MultiIndex.from_product([dates, symbols], names=["date", "symbol"])
    frame = index.to_frame(index=False)
    n = len(frame)

    signal = rng.normal(size=n)
    noise = rng.normal(size=n)
    frame["feat_signal"] = signal
    frame["mom_12_1_z"] = rng.normal(size=n)
    frame["reversal_5d_z"] = rng.normal(size=n)
    frame["value_score_z"] = rng.normal(size=n)
    frame["quality_decile"] = rng.integers(1, 11, size=n).astype("float64")
    frame["sector"] = [SECTORS[i % len(SECTORS)] for i in range(n)]
    frame["industry"] = frame["sector"] + " sub"
    frame["days_since_available"] = rng.integers(0, 90, size=n).astype("float64")
    frame["fundamental_available_from"] = pd.Timestamp("2023-06-30")

    # Deny-listed on purpose, so `build_features` has something to drop.
    frame["adj_close"] = rng.uniform(10, 400, size=n)
    frame["market_cap"] = rng.uniform(1e9, 1e12, size=n)
    # The regime label: identical across symbols on a date, which is what makes it a
    # clean regime cut and a useless feature.
    per_date = pd.Series(rng.uniform(0.05, 0.4, size=n_dates), index=dates)
    frame["market_vol_63d"] = frame["date"].map(per_date)

    # Targets. Weak but real dependence on feat_signal.
    frame["fwd_ret_5d_excess"] = 0.004 * signal + 0.02 * noise
    frame["fwd_ret_5d"] = frame["fwd_ret_5d_excess"] + 0.001
    frame["fwd_ret_21d"] = frame["fwd_ret_5d_excess"] * 4
    frame["label_up_5d"] = (frame["fwd_ret_5d_excess"] > 0).astype("int8")
    frame["fwd_ret_5d_xs_decile"] = (
        frame.groupby("date", observed=True)["fwd_ret_5d_excess"]
        .rank(pct=True)
        .mul(10)
        .clip(1, 10)
        .round()
        .astype("int8")
    )
    # One monthly fold per 21 dates, numbered so the last few land past eval_end_fold.
    day_number = frame["date"].rank(method="dense").astype(int) - 1
    frame["fold_id"] = (day_number // 21) + 1
    return frame


@pytest.fixture
def panel() -> pd.DataFrame:
    return make_panel()


@pytest.fixture
def config(tmp_path: Path) -> ModelingConfig:
    """A run config sized for the miniature panel."""
    return ModelingConfig(
        source={
            "db_schema": "gold",
            "table": "mart_training_set",
            "db_name": "aurum",
            "host": "HOST",
            "port": "PORT",
            "username": "AURUM_USERNAME",
            "password": "AURUM_PASSWORD",
        },
        target="fwd_ret_5d_excess",
        cache={"dir": tmp_path / "cache", "enabled": False},
        preprocess={"warmup_bars": 0, "min_cross_section": 1},
        splits={
            "horizon": 5,
            "embargo": 2,
            "burn_in_folds": 1,
            "eval_end_fold": 3,
            "refit_every": 1,
        },
        train={"grid": [{"n_estimators": 20, "early_stopping_rounds": 5,
                         "min_child_samples": 20, "num_leaves": 7}]},
        select={"sample_rows": 400, "n_era_blocks": 2, "max_features": 3,
                "seed_path": tmp_path / "seeds" / "selected_features.csv"},
        backtest={"n_shuffles": 8, "cost_bps_grid": [0.0, 10.0]},
        output_dir=tmp_path / "models",
    )


def prepared(panel: pd.DataFrame, config: ModelingConfig):
    """The frame and matrix the run directory below was built from."""
    frame, _ = filter_rows(panel, config.target, config.preprocess)
    frame = add_indicators(frame)
    matrix, manifest = build_features(frame, config.preprocess)
    return frame, matrix, manifest


@pytest.fixture
def saved_run(panel, config) -> str:
    """Train a small booster on the panel and write a complete run directory.

    Real rather than mocked: `evaluate`, `select-features` and `backtest` all call
    `booster.predict` (twice over, once with `pred_contrib=True`), and a stub that
    returns the right shape would not exercise the manifest replay that catches a
    reordered feature matrix.
    """
    import lightgbm as lgb

    from src.modeling.models.registry import save_run

    frame, matrix, manifest = prepared(panel, config)
    is_train = (frame["fold_id"] <= config.splits.eval_end_fold).to_numpy()
    booster = lgb.train(
        {"objective": "regression", "num_leaves": 7, "min_child_samples": 20,
         "verbosity": -1, "seed": 42},
        lgb.Dataset(matrix[is_train], label=frame.loc[is_train, config.target]),
        num_boost_round=15,
    )

    version = "20260907-testrun"
    metadata = {
        "version": version,
        "git_sha": "testsha",
        "target": config.target,
        "params": {"objective": "regression", "num_leaves": 7, "min_child_samples": 20,
                   "verbosity": -1, "n_estimators": 15},
        "n_configs_tried": 3,
        "final_n_estimators": 15,
        "mean_validation_ic": 0.01,
        "folds": [
            {"fold_index": 0, "valid_start_fold": 2, "valid_end_fold": 2, "best_ic": 0.02},
            {"fold_index": 1, "valid_start_fold": 3, "valid_end_fold": 3, "best_ic": 0.01},
        ],
    }
    directory = save_run(config.output_dir, booster, metadata, manifest, {"rows_in": len(panel)})
    assert json.loads((directory / "metadata.json").read_text())["version"] == version
    return version


@pytest.fixture
def fold_predictions(config, saved_run, panel):
    """The optional per-fold artifact, written beside the run."""
    from src.modeling.evaluate.runner import FOLD_PREDICTIONS

    frame, _, _ = prepared(panel, config)
    evaluation = frame[frame["fold_id"].between(2, config.splits.eval_end_fold)]
    predictions = pd.DataFrame(
        {
            "fold_index": (evaluation["fold_id"] - 2).to_numpy(),
            "symbol": evaluation["symbol"].to_numpy(),
            "date": evaluation["date"].to_numpy(),
            "y": evaluation[config.target].to_numpy(),
            "pred": evaluation["feat_signal"].to_numpy(),
        }
    )
    path = (config.output_dir / saved_run).resolve() / FOLD_PREDICTIONS
    predictions.to_parquet(path, index=False)
    return path


@pytest.fixture
def loaded(monkeypatch, panel):
    """Serve the miniature panel wherever the code would reach Postgres."""
    from src.modeling.data import loader
    from src.modeling.evaluate import runner as evaluate_runner

    monkeypatch.setattr(loader, "load_training_frame", lambda config: panel.copy())
    monkeypatch.setattr(evaluate_runner, "load_training_frame", lambda config: panel.copy())
    return panel
