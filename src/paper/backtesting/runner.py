"""Run the chosen engines on one shared schedule and save the run."""

import logging
import time
from pathlib import Path

import pandas as pd

from src.paper.backtest import fetch_bars, holdout_scores, simulate
from src.paper.backtesting import compare, output
from src.paper.backtesting.inputs import EngineResult, target_schedule
from src.paper.config import PaperConfig

logger = logging.getLogger(__name__)

ENGINES = ("sim", "vectorbt", "backtrader", "lumibot")


def _run_engine(name: str, config: PaperConfig, scores, bars, schedule) -> EngineResult:
    started = time.time()
    try:
        if name == "sim":
            equity, _, orders = simulate(config, scores, bars)
            return EngineResult("sim", equity, orders, note="in-house SimBroker")
        if name == "vectorbt":
            from src.paper.backtesting import vectorbt_engine as engine
        elif name == "backtrader":
            from src.paper.backtesting import backtrader_engine as engine
        elif name == "lumibot":
            from src.paper.backtesting import lumibot_engine as engine
        else:
            raise ValueError(f"unknown engine {name!r}; choose from {ENGINES}")
        return engine.run(config, schedule, bars)
    except ImportError as error:
        return EngineResult(name, error=f"not installed ({error}); run `uv sync --group backtesting`")
    except Exception as error:  # noqa: BLE001 - one broken engine must not lose the others
        logger.exception("%s failed", name)
        return EngineResult(name, error=f"{type(error).__name__}: {error}")
    finally:
        logger.info("%s finished in %.1fs", name, time.time() - started)


def run_all(
    config: PaperConfig,
    start: str,
    end: str,
    engines: list[str],
    periods: list[str],
    scores_file: str | None = None,
    bars_file: str | None = None,
) -> Path:
    scores = pd.read_parquet(scores_file) if scores_file else holdout_scores(config)
    scores = scores[(scores["date"] >= pd.Timestamp(start)) & (scores["date"] <= pd.Timestamp(end))]
    if scores.empty:
        raise ValueError(f"No scores between {start} and {end}")
    if bars_file:
        bars = pd.read_parquet(bars_file)
    else:
        bars = fetch_bars(config, sorted({*scores["symbol"].unique(), config.benchmark}), start, end)

    schedule = target_schedule(config, scores, pd.DatetimeIndex(sorted(bars["timestamp"].unique())))
    if not schedule:
        raise ValueError("No executions scheduled; do the bars cover the score dates?")

    results = [_run_engine(n, config, scores, bars, schedule) for n in engines]
    closes = bars[bars["symbol"] == config.benchmark].set_index("timestamp")["close"].sort_index()
    run_dir = output.write_run(config, results, closes, periods, start, end)

    ok = [r for r in results if r.error is None and len(r.equity)]
    if ok:
        print(output._fmt(compare.agreement(results, config.capital)).to_string(index=False))
    for r in results:
        if r.error:
            print(f"SKIPPED {r.name}: {r.error}")
    print(f"Saved {run_dir}\nReport: {run_dir / 'report.html'}")
    return run_dir
