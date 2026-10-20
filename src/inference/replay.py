"""Backtest the decision rule on the holdout, beside a momentum baseline.

The decisions are deciles of the model's score, so the book they describe is the one
``backtest.engine`` already simulates. This reruns that book from the decision labels and
adds what the engine does not report: what BUY, HOLD and SELL names went on to earn, and
the same numbers for the one-feature baseline the model has to beat.
"""

import asyncio
import json
import logging
from pathlib import Path

import pandas as pd

from src.inference import decide as decisions
from src.inference.resolve import resolve_run
from src.modeling.backtest import engine
from src.modeling.config import load_config
from src.modeling.evaluate.runner import _panel
from src.modeling.models.registry import load_run

logger = logging.getLogger(__name__)

REPLAY = "decisions_replay.json"
BASELINE_FEATURE = "mom_12_1_z"


def _outcomes(predictions: pd.DataFrame) -> dict:
    """Mean realised 5d excess return of each action's names, and the BUY-SELL gap."""
    labelled = decisions.assign_actions(
        predictions.rename(columns={"pred": "score"})[["symbol", "date", "score"]]
    ).merge(predictions[["symbol", "date", "y"]], on=["symbol", "date"])
    grouped = labelled.groupby("action")["y"]
    mean = grouped.mean()
    return {
        "mean_excess_return_5d": {a: float(mean.get(a, float("nan"))) for a in ("BUY", "HOLD", "SELL")},
        "n_decisions": {a: int(grouped.size().get(a, 0)) for a in ("BUY", "HOLD", "SELL")},
        "buy_minus_sell": float(mean.get("BUY", float("nan")) - mean.get("SELL", float("nan"))),
        "buy_hit_rate": float((labelled.loc[labelled["action"] == "BUY", "y"] > 0).mean()),
    }


def _summarise(name: str, predictions: pd.DataFrame, horizon: int, grid: list[float]) -> dict:
    book = engine.build_book(predictions, horizon)
    sweep = engine.sweep(book, grid)
    return {
        "name": name,
        "n_dates": int(len(book.spread)),
        "mean_turnover_per_rebalance": float(book.turnover.mean()),
        **sweep,
        **_outcomes(predictions),
    }


def run_replay(config_path: str, version: str) -> Path:
    config = load_config(config_path)
    directory = asyncio.run(resolve_run(config.output_dir, version, config.export))
    booster, manifest = load_run(directory.parent, directory.name)

    frame, matrix = _panel(config, manifest)
    holdout = (frame["fold_id"] > config.splits.eval_end_fold).to_numpy()
    if not holdout.any():
        raise ValueError(f"No rows past fold {config.splits.eval_end_fold}")

    base = pd.DataFrame(
        {
            "date": frame.loc[holdout, "date"].to_numpy(),
            "symbol": frame.loc[holdout, "symbol"].to_numpy(),
            "y": frame.loc[holdout, config.target].to_numpy(dtype="float64"),
        }
    )
    horizon, grid = config.splits.horizon, config.backtest.cost_bps_grid
    model = base.assign(pred=booster.predict(matrix[holdout]))
    momentum = base.assign(pred=frame.loc[holdout, BASELINE_FEATURE].to_numpy(dtype="float64"))

    report = {
        "version": directory.name,
        "rule": "decile 10 BUY, decile 1 SELL, deciles 2-9 HOLD (per date)",
        "start_date": str(pd.Timestamp(base["date"].min()).date()),
        "end_date": str(pd.Timestamp(base["date"].max()).date()),
        "model": _summarise("model", model, horizon, grid),
        "baseline": _summarise(f"baseline_{BASELINE_FEATURE}", momentum, horizon, grid),
    }
    out = directory / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    path = out / REPLAY
    path.write_text(json.dumps(report, indent=2, default=str))

    for block in ("model", "baseline"):
        b = report[block]
        print(
            f"{b['name']:24} Sharpe "
            + " ".join(f"{k}={v['sharpe']:.2f}" for k, v in b["by_cost_bps"].items())
            + f"  break-even {b['break_even_bps']:.1f} bps"
            + f"  turnover {b['mean_turnover_per_rebalance']:.2f}"
            + f"  BUY-SELL {b['buy_minus_sell'] * 1e4:.1f} bps/5d"
        )
    logger.info("Wrote %s", path)
    return path
