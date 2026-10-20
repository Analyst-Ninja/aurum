"""Score the newest trading day with a trained run and emit BUY / SELL / HOLD.

One path for the CLI and the MCP tools. Reads ``mart_features`` for a single date — a few
hundred rows — rather than the cached multi-million-row training panel, so it fits on the
small EC2 host. Writes nothing to the database; decisions are emitted, never traded.

Everything that waits on the network is awaited: the S3 lookup (aiobotocore) and the
Postgres read (asyncpg) run together, so a call costs the slower of the two rather than
their sum. Feature building and LightGBM scoring are CPU work with no async library, so
they run in a worker thread and never hold the event loop.
"""

import asyncio
import json
import logging
import os
import threading
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.inference import decide as decisions
from src.inference.resolve import DEFAULT_VERSION, resolve_run
from src.modeling.config import ModelingConfig, load_config
from src.modeling.data.loader import _validate_identifier, build_panel_query
from src.modeling.models.registry import load_run
from src.utils.env import load_env

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = "src/modeling/configs/lgbm_xs_excess_5d_narrow.yaml"
# Past a long weekend plus a holiday, an old as-of means the warehouse build is behind,
# and a stale ranking would be acted on as if it were today's.
STALE_AFTER_DAYS = 5
MAX_ROWS = 600
DEFAULT_TIMEOUT_S = 60.0

# One scoring at a time. Feature building plus a LightGBM load is a few hundred MB, and the
# MCP host has 1 GiB; a threading primitive rather than an asyncio one because it binds to
# no event loop, so it is also safe across repeated `asyncio.run` calls.
_SCORING = threading.Semaphore(1)


def check_fresh(asof: pd.Timestamp, today: date | None = None) -> None:
    age = ((today or date.today()) - asof.date()).days
    if age > STALE_AFTER_DAYS:
        raise RuntimeError(
            f"Latest features are dated {asof.date()}, {age} days old "
            f"(limit {STALE_AFTER_DAYS}). Has the dbt build run?"
        )


def make_engine(source) -> AsyncEngine:
    """Async engine from the config's env var names (the CLI path; MCP brings its own)."""
    load_env()
    url = URL.create(
        "postgresql+asyncpg",
        username=os.getenv(source.username),
        password=os.getenv(source.password),
        host=os.getenv(source.host),
        port=int(os.getenv(source.port, "5432")),
        database=source.db_name,
    )
    return create_async_engine(url, pool_size=1)


async def _declared_types(engine: AsyncEngine, schema: str, table: str) -> dict[str, str]:
    query = text(
        "select column_name, data_type from information_schema.columns "
        "where table_schema = :schema and table_name = :table order by ordinal_position"
    )
    async with engine.connect() as connection:
        rows = (await connection.execute(query, {"schema": schema, "table": table})).all()
    if not rows:
        raise ValueError(f"{schema}.{table} does not exist")
    return dict(rows)


def _typed(rows: list, columns: list[str], schema: pa.Schema) -> pd.DataFrame:
    """Rows as the dtypes the model trained on.

    Cast to the Arrow schema the training loader uses, not inferred: a column that is null
    for the whole day arrives as ``object`` and LightGBM would reject it, and float32 is
    what the cached training panel held.
    """
    frame = pd.DataFrame(rows, columns=columns)
    for field in schema:
        if pa.types.is_floating(field.type):
            frame[field.name] = frame[field.name].astype("float32")
        elif pa.types.is_timestamp(field.type):
            frame[field.name] = pd.to_datetime(frame[field.name])
    return frame


async def load_day(engine: AsyncEngine, config: ModelingConfig, asof: str | None = None) -> pd.DataFrame:
    """Feature rows for the last date on or before ``asof`` (default: the newest)."""
    schema = _validate_identifier(config.source.db_schema)
    table = _validate_identifier(config.source.predict_table)
    qualified = f'"{schema}"."{table}"'
    query, arrow_schema = build_panel_query(await _declared_types(engine, schema, table), qualified)

    # asyncpg binds a date, not a string; fromisoformat also rejects a malformed --asof here.
    bound = {} if asof is None else {"asof": date.fromisoformat(asof)}
    limit = "" if asof is None else ' and "date" <= cast(:asof as date)'
    sql = text(f'{query} where "date" = (select max("date") from {qualified} where true{limit})')
    async with engine.connect() as connection:
        result = await connection.execute(sql, bound)
        frame = _typed(result.fetchall(), list(result.keys()), arrow_schema)
    if frame.empty:
        raise ValueError(f"{qualified} has no rows" + (f" on or before {asof}" if asof else ""))
    return frame


@lru_cache(maxsize=2)
def _load_run_cached(root: str, name: str) -> tuple[Any, dict[str, Any]]:
    """Booster and manifest by resolved directory: a new run is a new key."""
    return load_run(Path(root), name)


def _holdout_caveat(directory: Path) -> str:
    base = "Relative ranking against the day's universe, not a price forecast."
    summary = directory / "backtest" / "summary.json"
    if not summary.is_file():
        return base
    holdout = next(iter(json.loads(summary.read_text())["blocks"].values()))
    sharpe = holdout["by_cost_bps"].get("10bps", {}).get("sharpe")
    if sharpe is None:
        return base
    return (
        f"{base} Holdout {holdout['start_date']} to {holdout['end_date']}: long-short "
        f"Sharpe {sharpe:.2f} net of 10 bps, break-even {holdout['break_even_bps']:.0f} bps."
    )


async def _prepare(config: ModelingConfig, version: str) -> tuple[Path, Any, dict[str, Any]]:
    """Resolve the run (S3 or disk) and load the booster."""
    directory = await resolve_run(config.output_dir, version, config.export)
    booster, manifest = await asyncio.to_thread(_load_run_cached, str(directory.parent), directory.name)
    return directory, booster, manifest


def _finish(
    directory: Path,
    booster: Any,
    manifest: dict[str, Any],
    frame: pd.DataFrame,
    config: ModelingConfig,
    check: bool,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """The CPU half: staleness check, feature build, scoring, decisions."""
    day = pd.Timestamp(frame["date"].iloc[0])
    if check:
        check_fresh(day)
    with _SCORING:
        scores = decisions.score_frame(booster, manifest, frame, config.preprocess)
    result = decisions.assign_actions(scores)
    meta = {
        "version": directory.name,
        "asof": str(day.date()),
        "universe": int(result["score"].notna().sum()),
        "caveat": _holdout_caveat(directory),
    }
    return result, meta


def _timeout() -> float:
    return float(os.getenv("AURUM_INFERENCE_TIMEOUT_S", DEFAULT_TIMEOUT_S))


async def decide_day(
    engine: AsyncEngine,
    config: ModelingConfig,
    version: str = DEFAULT_VERSION,
    asof: str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Decisions for one date, plus the metadata a reader needs to trust them.

    The run lookup and the database read are independent, so they are awaited together.
    On timeout the caller gets an error at once; a scoring thread already started cannot
    be cancelled and finishes in the background, which `_SCORING` keeps to one at a time.
    """
    seconds = _timeout()
    try:
        async with asyncio.timeout(seconds):
            (directory, booster, manifest), frame = await asyncio.gather(
                _prepare(config, version), load_day(engine, config, asof)
            )
            return await asyncio.to_thread(
                _finish, directory, booster, manifest, frame, config, asof is None
            )
    except TimeoutError:
        raise RuntimeError(f"decision scoring timed out after {seconds:g}s") from None


def to_payload(
    result: pd.DataFrame,
    meta: dict[str, Any],
    action: str | None = None,
    symbols: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """JSON-safe view of ``result``. Deciles come from the full day, filters come after."""
    view = result
    if symbols is not None:
        wanted = {s.upper() for s in symbols}
        view = view[view["symbol"].str.upper().isin(wanted)]
    if action is not None:
        if action.upper() not in (decisions.BUY, decisions.SELL, decisions.HOLD):
            raise ValueError(f"action must be BUY, SELL or HOLD, got {action!r}")
        view = view[view["action"] == action.upper()]
    if action and action.upper() == decisions.SELL:
        view = view.sort_values("score", ascending=True)
    view = view.head(max(1, min(limit, MAX_ROWS)))

    rows = [
        {
            "symbol": r.symbol,
            "action": r.action,
            "decile": None if pd.isna(r.decile) else int(r.decile),
            "rank": None if pd.isna(r.rank) else int(r.rank),
            "score": None if pd.isna(r.score) else round(float(r.score), 4),
        }
        for r in view.itertuples()
    ]
    return {**meta, "rows": rows}


def write_decisions(result: pd.DataFrame, meta: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{meta['asof']}.csv"
    result.assign(version=meta["version"]).to_csv(path, index=False)
    (out_dir / "latest.csv").write_bytes(path.read_bytes())
    return path


async def arun_decide(config_path: str, version: str, asof: str | None, out_dir: Path) -> Path:
    config = load_config(config_path)
    engine = make_engine(config.source)
    try:
        result, meta = await decide_day(engine, config, version, asof)
    finally:
        await engine.dispose()

    path = await asyncio.to_thread(write_decisions, result, meta, out_dir)
    print(f"{meta['version']}  as of {meta['asof']}  ({meta['universe']} symbols)")
    for name in (decisions.BUY, decisions.SELL):
        picked = result[result["action"] == name]
        label = ", ".join(picked.sort_values("rank", ascending=name == decisions.BUY)["symbol"])
        print(f"{name:4} ({len(picked)}): {label}")
    print(meta["caveat"])
    logger.info("Wrote %s", path)
    return path


def run_decide(config_path: str, version: str, asof: str | None, out_dir: Path) -> Path:
    """Blocking entry point for the CLI."""
    return asyncio.run(arun_decide(config_path, version, asof, out_dir))
