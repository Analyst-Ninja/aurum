import asyncio
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from src.inference import decide, replay, resolve, run
from src.inference.resolve import resolve_run
from src.modeling.backtest import portfolio
from src.modeling.config import ExportConfig


def _scores(n_dates=2, n_symbols=20, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2026-01-05", periods=n_dates)
    frame = pd.MultiIndex.from_product([dates, [f"S{i:02d}" for i in range(n_symbols)]],
                                       names=["date", "symbol"]).to_frame(index=False)
    frame["score"] = rng.normal(size=len(frame))
    return frame


def test_top_decile_buys_bottom_decile_sells_per_date():
    out = decide.assign_actions(_scores())

    per_date = out.groupby("date")["action"].value_counts().unstack()
    assert (per_date["BUY"] == 2).all() and (per_date["SELL"] == 2).all()
    assert (per_date["HOLD"] == 16).all()
    buys = out[out["action"] == "BUY"]
    assert (buys["rank"] <= 2).all()


def test_a_null_score_is_hold_with_no_decile():
    scores = _scores(n_dates=1)
    scores.loc[0, "score"] = np.nan

    out = decide.assign_actions(scores).set_index("symbol")

    assert out.loc["S00", "action"] == "HOLD"
    assert pd.isna(out.loc["S00", "decile"]) and pd.isna(out.loc["S00", "rank"])


def test_the_buy_set_is_the_backtest_long_leg():
    preds = _scores(n_dates=6).rename(columns={"score": "pred"}).assign(y=0.0)
    labelled = decide.assign_actions(preds.rename(columns={"pred": "score"}))
    positions = portfolio.build_positions(preds, horizon=5)

    for day, group in labelled.groupby("date"):
        longs = set(positions[(positions["date"] == day) & (positions["weight"] > 0)]["symbol"])
        assert set(group[group["action"] == "BUY"]["symbol"]) == longs


def test_a_reordered_feature_matrix_is_refused(saved_run, config, panel):
    from src.modeling.models.registry import load_run

    booster, manifest = load_run(config.output_dir, saved_run)
    manifest = {**manifest, "features": manifest["features"][::-1]}

    with pytest.raises(ValueError, match="does not match the manifest"):
        decide.score_frame(booster, manifest, panel.tail(30), config.preprocess)


def test_resolve_prefers_the_registry_entry_then_the_newest_local(tmp_path):
    for name in ("20260801-aaa-narrow", "20260901-bbb-narrow", "20260905-ccc-full"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "model.txt").write_text("m")

    assert asyncio.run(resolve_run(tmp_path, "latest-narrow")).name == "20260901-bbb-narrow"
    assert asyncio.run(resolve_run(tmp_path, "latest")).name == "20260905-ccc-full"
    (tmp_path / "latest-narrow").symlink_to("20260801-aaa-narrow")
    assert asyncio.run(resolve_run(tmp_path, "latest-narrow")).name == "20260801-aaa-narrow"


def test_resolve_without_a_run_or_bucket_says_so(tmp_path, monkeypatch):
    monkeypatch.delenv("AURUM_ARTIFACTS_BUCKET", raising=False)
    with pytest.raises(FileNotFoundError, match="no artifacts bucket"):
        asyncio.run(resolve_run(tmp_path, "latest-narrow"))


class _Body:
    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self):
        return self.data


class _S3:
    """Just enough of an aiobotocore client: two versions, two exports of the newer one."""

    class exceptions:  # noqa: N801
        class NoSuchKey(Exception):
            pass

    keys = {
        "runs/20260801-aaa-narrow/2026-08-01T000000Z/": ["model.txt"],
        "runs/20260901-bbb-narrow/2026-09-01T000000Z/": ["old"],
        "runs/20260901-bbb-narrow/2026-09-02T000000Z/": [
            "metadata.json", "feature_manifest.json", "preprocess_manifest.json", "model.txt"],
        "runs/20260905-ccc-full/2026-09-05T000000Z/": ["model.txt"],
    }

    def get_paginator(self, _):
        return self

    async def paginate(self, Bucket, Prefix, Delimiter):  # noqa: N803
        seen = sorted({k[len(Prefix):].split("/")[0] + "/" for k in self.keys if k.startswith(Prefix)})
        yield {"CommonPrefixes": [{"Prefix": Prefix + s} for s in seen]}

    async def get_object(self, Bucket, Key):  # noqa: N803
        prefix, name = Key.rsplit("/", 1)
        if name not in self.keys.get(prefix + "/", []):
            raise self.exceptions.NoSuchKey()
        return {"Body": _Body(f"{Key}".encode())}


def _fake_s3(monkeypatch, client=None):
    client = client or _S3()

    @asynccontextmanager
    async def factory():
        yield client

    monkeypatch.setattr(resolve, "_s3_client", factory)
    return client


def test_resolve_downloads_the_newest_export_of_the_newest_narrow_run(tmp_path, monkeypatch):
    _fake_s3(monkeypatch)

    directory = asyncio.run(resolve_run(tmp_path, "latest-narrow", ExportConfig(bucket="b")))

    assert directory.name == "20260901-bbb-narrow"
    assert (directory / "model.txt").read_text().endswith("2026-09-02T000000Z/model.txt")
    assert not (directory / "backtest" / "summary.json").exists()


def test_a_missing_required_file_leaves_no_half_downloaded_run(tmp_path, monkeypatch):
    client = _S3()
    client.keys = {"runs/20260901-bbb-narrow/2026-09-02T000000Z/": ["model.txt"]}
    _fake_s3(monkeypatch, client)

    with pytest.raises(FileNotFoundError, match="metadata.json"):
        asyncio.run(resolve_run(tmp_path, "latest-narrow", ExportConfig(bucket="b")))

    assert not (tmp_path / "20260901-bbb-narrow" / "model.txt").exists()


def test_stale_features_are_refused():
    with pytest.raises(RuntimeError, match="days old"):
        run.check_fresh(pd.Timestamp("2026-09-01"), today=date(2026, 9, 10))
    run.check_fresh(pd.Timestamp("2026-09-04"), today=date(2026, 9, 8))  # a long weekend


@pytest.fixture
def served(monkeypatch, saved_run, config, panel):
    """Serve the last day of the panel wherever the code would reach Postgres."""
    last = panel[panel["date"] == panel["date"].max()].drop(
        columns=[c for c in panel.columns if c.startswith("fwd_") or c.startswith("label_")]
    )

    async def load_day(engine, cfg, asof=None):
        return last.copy()

    monkeypatch.setattr(run, "load_day", load_day)
    return saved_run, config


def _decide(config, version, **kwargs):
    return asyncio.run(run.decide_day(None, config, version, **kwargs))


def test_decide_day_labels_the_newest_date(served):
    version, config = served

    result, meta = _decide(config, version, asof="2024-05-01")

    assert meta["version"] == version and meta["universe"] == 24
    assert set(result["action"]) == {"BUY", "SELL", "HOLD"}
    assert "Relative ranking" in meta["caveat"]


def test_decide_day_refuses_an_old_date_unless_asked_for_it(served):
    version, config = served
    with pytest.raises(RuntimeError, match="days old"):
        _decide(config, version)


def test_filtering_to_symbols_keeps_deciles_from_the_whole_day(served):
    version, config = served
    result, meta = _decide(config, version, asof="2024-05-01")

    whole = {r["symbol"]: r for r in run.to_payload(result, meta, limit=600)["rows"]}
    some = run.to_payload(result, meta, symbols=["sym03", "SYM07"])["rows"]

    assert [r["symbol"] for r in some] and all(r == whole[r["symbol"]] for r in some)
    with pytest.raises(ValueError, match="BUY, SELL or HOLD"):
        run.to_payload(result, meta, action="short")


def test_cli_output_is_written_and_latest_points_at_it(served, tmp_path):
    version, config = served
    result, meta = _decide(config, version, asof="2024-05-01")

    path = run.write_decisions(result, meta, tmp_path / "out")

    assert path.name == f"{meta['asof']}.csv"
    assert (tmp_path / "out" / "latest.csv").read_text() == path.read_text()


def test_the_run_lookup_and_the_database_read_overlap(served, monkeypatch):
    """Each side waits for the other to start; sequential code would time out."""
    version, config = served
    original = run._prepare
    last = asyncio.run(run.load_day(None, config))

    async def go():
        prep_started, load_started = asyncio.Event(), asyncio.Event()

        async def prepare(cfg, v):
            prep_started.set()
            await asyncio.wait_for(load_started.wait(), 1)
            return await original(cfg, v)

        async def load_day(engine, cfg, asof=None):
            load_started.set()
            await asyncio.wait_for(prep_started.wait(), 1)
            return last.copy()

        monkeypatch.setattr(run, "_prepare", prepare)
        monkeypatch.setattr(run, "load_day", load_day)
        return await run.decide_day(None, config, version, asof="2024-05-01")

    result, _ = asyncio.run(go())
    assert len(result) == 24


def test_scoring_does_not_hold_the_event_loop(served, monkeypatch):
    version, config = served
    original = run._finish

    def slow(*args):
        time.sleep(0.3)
        return original(*args)

    monkeypatch.setattr(run, "_finish", slow)

    async def go():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        task = asyncio.create_task(ticker())
        await run.decide_day(None, config, version, asof="2024-05-01")
        task.cancel()
        return ticks

    assert asyncio.run(go()) >= 5


def test_only_one_scoring_runs_at_a_time(served, monkeypatch):
    version, config = served
    original = decide.score_frame
    lock, active, peak = threading.Lock(), 0, 0

    def tracked(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.1)
        try:
            return original(*args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(decide, "score_frame", tracked)

    async def go():
        await asyncio.gather(
            *(run.decide_day(None, config, version, asof="2024-05-01") for _ in range(3))
        )

    asyncio.run(go())
    assert peak == 1


def test_a_slow_call_times_out_with_a_clear_error(served, monkeypatch):
    version, config = served
    monkeypatch.setenv("AURUM_INFERENCE_TIMEOUT_S", "0.05")

    async def slow_load(engine, cfg, asof=None):
        await asyncio.sleep(1)

    monkeypatch.setattr(run, "load_day", slow_load)

    with pytest.raises(RuntimeError, match="timed out after 0.05s"):
        _decide(config, version, asof="2024-05-01")


class _Result:
    def __init__(self, rows, columns=()):
        self.rows, self.columns = rows, list(columns)

    def all(self):
        return self.rows

    def fetchall(self):
        return self.rows

    def keys(self):
        return self.columns


class _Connection:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, params=None):
        self.log.append((str(statement), params))
        if "information_schema" in str(statement):
            return _Result([("date", "date"), ("symbol", "text"), ("x", "numeric"), ("y", "numeric")])
        return _Result(
            [(datetime(2026, 9, 4), "AAA", 1.5, None), (datetime(2026, 9, 4), "BBB", None, None)],
            ["date", "symbol", "x", "y"],
        )


class _Engine:
    def __init__(self):
        self.log = []

    def connect(self):
        return _Connection(self.log)


def test_load_day_returns_the_dtypes_the_model_trained_on(config):
    engine = _Engine()

    frame = asyncio.run(run.load_day(engine, config, asof="2026-09-04"))

    # `y` is null for the whole day: inferred it would be `object`, which LightGBM rejects.
    assert str(frame["x"].dtype) == "float32" and str(frame["y"].dtype) == "float32"
    assert str(frame["date"].dtype).startswith("datetime64")
    sql, params = engine.log[-1]
    assert "mart_features" in sql and params == {"asof": date(2026, 9, 4)}


def test_load_day_rejects_a_malformed_date_before_touching_the_database(config):
    with pytest.raises(ValueError, match="Invalid isoformat"):
        asyncio.run(run.load_day(_Engine(), config, asof="yesterday"))


def test_the_mcp_tools_return_the_same_rows(served, monkeypatch):
    from src.mcp import db, server

    version, config = served
    monkeypatch.setattr(db, "get_async_engine", lambda: None)
    monkeypatch.setattr("src.modeling.config.load_config", lambda path: config)

    async def decided(engine, cfg, v, asof):
        return _decided(version, config)

    monkeypatch.setattr(run, "decide_day", decided)

    out = asyncio.run(server.mcp.call_tool("score_symbols", {"symbols": ["SYM01"]}))

    rows = out.structured_content["rows"] if hasattr(out, "structured_content") else out[1]["rows"]
    assert [r["symbol"] for r in rows] == ["SYM01"]


def test_a_stale_date_reaches_the_mcp_client_as_a_tool_error(served, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from src.mcp import db, server

    version, config = served
    monkeypatch.setattr(db, "get_async_engine", lambda: None)
    monkeypatch.setattr("src.modeling.config.load_config", lambda path: config)

    async def stale(engine, cfg, v, asof):
        raise RuntimeError("Latest features are dated 2026-08-01, 40 days old")

    monkeypatch.setattr(run, "decide_day", stale)

    with pytest.raises(ToolError, match="40 days old"):
        asyncio.run(server.mcp.call_tool("get_decisions", {}))


def _decided(version, config):
    scores = pd.DataFrame({"symbol": [f"SYM{i:02d}" for i in range(20)],
                           "date": pd.Timestamp("2026-09-04"),
                           "score": np.linspace(-1, 1, 20)})
    return decide.assign_actions(scores), {"version": version, "asof": "2026-09-04",
                                           "universe": 20, "caveat": "c"}


def test_replay_reports_the_model_next_to_the_momentum_baseline(
    saved_run, config, panel, monkeypatch, tmp_path
):
    from src.modeling.evaluate import runner

    monkeypatch.setattr(runner, "load_training_frame", lambda cfg: panel.copy())
    monkeypatch.setattr("src.inference.replay.load_config", lambda path: config)
    monkeypatch.setattr(replay, "_panel", runner._panel)

    path = replay.run_replay("ignored.yaml", saved_run)

    report = pd.read_json(path, typ="series")
    assert report["model"]["name"] == "model" and report["baseline"]["name"].startswith("baseline_")
    assert set(report["model"]["n_decisions"]) == {"BUY", "HOLD", "SELL"}
    assert "0bps" in report["model"]["by_cost_bps"]
