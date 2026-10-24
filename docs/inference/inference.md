# Inference — BUY / SELL / HOLD from the newest trained model

`src/inference/` turns the latest trained run into a daily decision list. Tracked in
[#97](https://github.com/Analyst-Ninja/aurum/issues/97). **Decisions are emitted, never traded by this module.** The separate [`src/paper`](../paper/paper-trading.md) module can turn them into Alpaca *paper* orders; AURUM never trades real money.

## What a decision means

The model predicts the **cross-sectional 5-day excess return** (`fwd_ret_5d_excess`), so its score
ranks S&P 500 stocks against each other on one day. It is not a price forecast.

| Rank that day (decile of score) | Action | Read it as |
|---|---|---|
| 10 (top) | **BUY** | expected to beat the market over 5 days |
| 2–9 | **HOLD** | no edge |
| 1 (bottom) | **SELL** | expected to lag the market — avoid / underweight, not necessarily short |
| score is null | **HOLD** | nothing to rank |

The decile is the same one the warehouse, `evaluate` and `backtest` use (`metrics.deciles`), so the
backtest's long leg is exactly the BUY list. The rule is stateless: it does not know what you hold.

## How much to trust it

Holdout 2024-10 → 2026-08, run `20260906-a7cffa3`: IC 0.007 (95% CI −0.022 to 0.036, includes zero),
long-short Sharpe 0.46 gross, 0.20 after 10 bps, break-even 18 bps. The one-feature
`mom_12_1_z` baseline scored higher (Sharpe 0.73) at a quarter of the turnover. Every output carries
the holdout line from `backtest/summary.json` as its `caveat`. `replay` re-runs this comparison for
the decision rule.

## Run it

```bash
# local — needs Postgres (gold.mart_features) and a run under models/
uv run --group modeling python -m src.inference.cli decide
uv run --group modeling python -m src.inference.cli decide --asof 2026-09-05   # history; skips the stale guard
uv run --group modeling python -m src.inference.cli replay                      # backtest the rule, with momentum baseline

# the pinned image
docker compose -f docker-compose.modeling.yml run --rm trainer infer decide
```

Options: `-c CONFIG` (default `lgbm_xs_excess_5d_narrow.yaml`), `--version` (default `latest-narrow`,
a version id also works), `--out DIR` (default `decisions/`, git-ignored).

`decide` writes `decisions/<asof>.csv` and `decisions/latest.csv` (`symbol, date, score, decile,
action, rank, version`) and prints the BUY and SELL lists.
`replay` writes `models/<version>/backtest/decisions_replay.json`: net Sharpe at each cost, break-even
bps, turnover, and the mean 5-day excess return of BUY / HOLD / SELL names, for the model and the
baseline.

**Which run is served.** In order: `models/<version>` (the `latest-narrow` symlink beside the trainer);
else, with `AURUM_ARTIFACTS_BUCKET` set, the newest export under `s3://<bucket>/runs/` whose version
id ends `-narrow`, downloaded once into `models/`; else the newest matching local directory.
Promotion stays manual: this serves the newest narrow run, not a vetted one.

**Stale guard.** With no `--asof`, `decide` exits non-zero if the newest feature date is more than 5
days old. A dbt build that failed otherwise yields a confident ranking of last week's market.

## From Claude (MCP)

Two tools on the MCP server, same code path and same output as the CLI:

- `get_decisions(asof=None, action=None, limit=50, version=None)`
- `score_symbols(symbols, asof=None, version=None)` — deciles are ranked against the whole day's
  universe, then filtered, so `AAPL` gets the same answer as in `get_decisions`.

They read `gold.mart_features` through the read-only role and write nothing. LightGBM is imported
inside the tool, so `run_query` and start-up cost the same as before. The config comes from
`AURUM_INFERENCE_CONFIG` (default: the narrow config).

## Concurrency

The predict path is async end to end (`run.decide_day`, `resolve.resolve_run`, the two MCP tools):

- **S3** is read with `aiobotocore` (aiohttp underneath) and **Postgres** with `asyncpg` through
  SQLAlchemy's async engine. The run lookup and the database read are awaited together, so a call costs
  the slower of the two, not their sum.
- Feature building and LightGBM scoring are CPU work with no async library; they run in a worker
  thread (`asyncio.to_thread`) so they never hold the event loop. At most one scoring runs at a time
  (memory on the 1 GiB host).
- `AURUM_INFERENCE_TIMEOUT_S` (default 60) bounds a call. A scoring thread already started cannot be
  cancelled and finishes in the background.
- `aiobotocore` rather than `aioboto3`: `aioboto3` pins an older botocore than this repo's `boto3`.
- The MCP SQL tools (`run_query` etc.) are unchanged; the SDK already runs them in a worker thread.
- `replay` is a batch job and stays synchronous (it wraps the resolver in `asyncio.run`).

## On the EC2 MCP host

The `--target mcp` image now includes the `modeling` group, `src/inference` and `src/modeling`, and the
host's instance role can read `runs/*` in the artifacts bucket; the unit passes
`AURUM_ARTIFACTS_BUCKET`. The first call downloads the model (≈0.1 MB) and later calls reuse it. A new
monthly run is picked up on the next call. **Check memory on first deploy:** LightGBM, pandas and scipy
add a few hundred MB to a server that idled at ~145 MB on a 1 GiB `t3.micro`; move
`mcp_instance_type` to `t3.small` if it swaps. Only the newest date is read, never the training panel.

## Not built

Turnover hysteresis (model turnover is 0.55 per rebalance, which is what costs money), position
sizing, a schedule, and automatic promotion.
