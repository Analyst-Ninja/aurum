"""Backtest a saved run and write ``models/{version}/backtest/``.

Two blocks are simulated. The **holdout** is the quoted result — 24 months the shipped
booster never saw. The **evaluation folds** are shown beside it because they are what
chose the hyperparameters, and a large gap between the two is the most useful single
diagnostic on the page; they are labelled `model_selection` so nobody quotes them.

The three reality checks run on the holdout only. That is where the claim lives, and
500 shuffles over the fold panel is twenty minutes spent stress-testing a number the
report already tells you not to quote.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from src.modeling.backtest import engine, portfolio, report
from src.modeling.config import load_config
from src.modeling.evaluate.runner import FOLD_PREDICTIONS, _panel
from src.modeling.models.registry import load_run

logger = logging.getLogger(__name__)

SUMMARY = "summary.json"
YEARLY = "yearly.csv"
EQUITY = "equity_curve.csv"
POSITIONS = "positions.parquet"
# The cost level the yearly table and the promotion conversation are quoted at.
HEADLINE_COST_BPS = 10.0


def _block(title: str, subtitle: str, predictions: pd.DataFrame, config) -> dict:
    """Simulate one prediction set across the cost grid."""
    horizon = config.splits.horizon
    book = engine.build_book(predictions, horizon)
    if book.spread.empty:
        raise ValueError(f"{title}: no date formed both decile legs")

    grid = config.backtest.cost_bps_grid
    curves = {
        f"{int(cost)} bps": engine.equity_curve(engine.run(book, cost), horizon)
        for cost in grid
    }
    net = engine.run(book, HEADLINE_COST_BPS)
    return {
        "title": title,
        "subtitle": subtitle,
        "predictions": predictions,
        "book": book,
        "curves": curves,
        "sweep": engine.sweep(book, grid),
        "yearly": engine.yearly_table(net, horizon),
        "mean_turnover": float(book.turnover.mean()) if len(book.turnover) else float("nan"),
        "n_dates": int(len(book.spread)),
        "start_date": str(pd.Timestamp(book.spread.index.min()).date()),
        "end_date": str(pd.Timestamp(book.spread.index.max()).date()),
    }


def _holdout_predictions(config, booster, manifest) -> pd.DataFrame:
    """Score the holdout exactly as the evaluation harness does."""
    frame, matrix = _panel(config, manifest)
    is_holdout = (frame["fold_id"] > config.splits.eval_end_fold).to_numpy()
    if not is_holdout.any():
        raise ValueError(f"No rows past fold {config.splits.eval_end_fold}")

    holdout = frame[is_holdout]
    return pd.DataFrame(
        {
            "date": holdout["date"].to_numpy(),
            "symbol": holdout["symbol"].to_numpy(),
            "y": holdout[config.target].to_numpy(dtype="float64"),
            "pred": booster.predict(matrix[is_holdout]),
        }
    )


def _reality_checks(block: dict, config, metadata: dict) -> tuple[dict, list[dict], dict]:
    """Randomization, signal lag and deflated Sharpe on the holdout book."""
    horizon = config.splits.horizon
    randomization = engine.randomization_test(
        block["predictions"], block["book"], config.backtest.n_shuffles, config.backtest.seed
    )
    lagged = engine.signal_lag_test(block["predictions"], horizon)
    gross = randomization["actual_sharpe"]
    deflated = engine.deflated_sharpe(
        gross, metadata.get("n_configs_tried", 1), block["n_dates"] // horizon
    )

    rows = [
        {
            "check": "Randomization",
            "value": f"{randomization['percentile']:.1f}th percentile "
            f"(p = {randomization['p_value']:.3f})",
            "reads": "Sharpe against a null with the same book and no signal",
        },
        {
            "check": "Signal lag (1 day)",
            "value": f"Sharpe {lagged['sharpe']:.2f} vs {gross:.2f} gross",
            "reads": "A collapse means same-bar information is leaking upstream",
        },
        {
            "check": "Deflated Sharpe",
            "value": f"{deflated:.3f} over {metadata.get('n_configs_tried', 1)} configs",
            "reads": "Probability the Sharpe beats the best of that many noise strategies",
        },
    ]
    checks = {
        "randomization": {k: v for k, v in randomization.items() if k != "null_sharpes"},
        "signal_lag": lagged,
        "deflated_sharpe": deflated,
    }
    return checks, rows, randomization


def run_backtest(config_path: str, version: str) -> Path:
    """Simulate `version`, write the artifact set, return the summary path."""
    config = load_config(config_path)
    booster, manifest = load_run(config.output_dir, version)
    directory = (config.output_dir / version).resolve()
    metadata = json.loads((directory / "metadata.json").read_text())
    horizon = config.splits.horizon

    holdout = _block(
        "Holdout — the quoted result",
        f"Folds {config.splits.eval_end_fold + 1}+, never seen by the shipped booster.",
        _holdout_predictions(config, booster, manifest),
        config,
    )
    blocks = [holdout]

    fold_path = directory / FOLD_PREDICTIONS
    if fold_path.exists():
        blocks.append(
            _block(
                "Evaluation folds — model selection, not a result",
                "Folds 121-297 chose the hyperparameters and the iteration count.",
                pd.read_parquet(fold_path),
                config,
            )
        )
    else:
        logger.warning("%s absent — backtesting the holdout only", fold_path)

    checks, check_rows, randomization = _reality_checks(holdout, config, metadata)
    holdout["randomization"] = randomization

    out = directory / "backtest"
    out.mkdir(parents=True, exist_ok=True)

    holdout["yearly"].to_csv(out / YEARLY, index=False)
    pd.DataFrame({label: curve for label, curve in holdout["curves"].items()}).to_csv(
        out / EQUITY
    )
    portfolio.build_positions(holdout["predictions"], horizon).to_parquet(
        out / POSITIONS, index=False
    )
    report.write_tearsheet(
        out / report.TEARSHEET,
        holdout["predictions"],
        holdout["curves"],
        holdout["yearly"],
        randomization,
    )

    summary = {
        "version": metadata.get("version", version),
        "backtested_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "horizon": horizon,
        "construction": {
            "legs": "long decile 10, short decile 1, dollar-neutral, equal-weight",
            "tranches": horizon,
            "headline_cost_bps": HEADLINE_COST_BPS,
        },
        "blocks": {
            block["title"]: {
                "start_date": block["start_date"],
                "end_date": block["end_date"],
                "n_dates": block["n_dates"],
                "mean_turnover_per_rebalance": block["mean_turnover"],
                **block["sweep"],
            }
            for block in blocks
        },
        "reality_checks": check_rows,
        "reality_checks_detail": checks,
        "assumptions": report.BIASES,
    }
    (out / SUMMARY).write_text(json.dumps(summary, indent=2, default=str))
    report.write_report(out / report.REPORT, metadata.get("version", version), blocks, summary)

    sweep = holdout["sweep"]
    logger.info(
        "Holdout Sharpe %.2f gross, %.2f at %s bps; break-even %.1f bps",
        sweep["by_cost_bps"]["0bps"]["sharpe"],
        sweep["by_cost_bps"][f"{int(HEADLINE_COST_BPS)}bps"]["sharpe"],
        int(HEADLINE_COST_BPS),
        sweep["break_even_bps"],
    )
    logger.info("Wrote %s", out / report.REPORT)
    return out / SUMMARY


def net_sharpe_at(summary_path: Path, cost_bps: float = HEADLINE_COST_BPS) -> float:
    """Holdout net Sharpe at `cost_bps`, for a promotion decision. Reads `summary.json`."""
    summary = json.loads(Path(summary_path).read_text())
    holdout = next(iter(summary["blocks"].values()))
    return float(holdout["by_cost_bps"][f"{int(cost_bps)}bps"]["sharpe"])
