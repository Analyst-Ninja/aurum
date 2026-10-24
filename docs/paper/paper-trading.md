# Paper trading and Alpaca-data backtest

Forward-tests the inference decisions with simulated money. Code in `src/paper/`, config in `src/paper/configs/default.yaml`, tracked in GH-101 (PR #102).

**Paper money only.** AURUM never trades real money. `AlpacaBroker` talks to Alpaca's *paper* environment, and decisions from [`inference`](../inference/inference.md) remain advice. This module is the one place that turns them into (simulated) orders. It is not scheduled on AWS: run `rebalance` yourself, once a day after the dbt build.

## Setup
1. Free Alpaca paper account; add `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` to `.env`.
2. `uv sync --group modeling --group paper --group backtesting` (the last group is only for the cross-check)

## Commands
```bash
uv run --group modeling --group paper python -m src.paper.cli rebalance --dry-run   # show orders, send nothing
uv run --group modeling --group paper python -m src.paper.cli rebalance             # once a day, after the dbt build
uv run --group paper python -m src.paper.cli snapshot --watch                       # equity every snapshot_interval_min
uv run --group paper python -m src.paper.cli report -p W,M,Q,Y
uv run --group modeling --group paper python -m src.paper.cli backtest --start 2024-01-01 --end 2026-10-01 --bar-interval 30Min -p W,M,Q,Y
uv run --group modeling --group paper --group backtesting python -m src.paper.cli crosscheck --start 2024-01-01 --end 2026-10-01 --bar-interval 1Day --cost-bps 0
```

## Cross-check on vectorbt, backtrader and Lumibot (`src/paper/backtesting/`)
`crosscheck` runs the same weight schedule through the in-house sim and the three libraries and saves everything to `data/paper/backtest/<UTC time>-<bar>/` (`latest` points at the newest):
`report.html` (agreement table, equity and drawdown charts, weekly/monthly/quarterly/yearly P&L per engine, skipped engines with errors), `report.md`, `params.json`, `summary.json`, `equity_<engine>.csv`, `orders_<engine>.csv`. `backtest` is the same with the sim only.

- All engines read one schedule (`inputs.target_schedule`), so a gap is an execution-maths difference. An engine more than 0.5 pp of total return from the sim is marked NO in the report.
- **Lumibot runs in a child process** (`uv run --with lumibot --with "mcp<2"`) because it needs `mcp<2` and `src/mcp` is written for mcp 2.x. It is daily-bars only. An order submitted in the iteration dated D fills at D's open, so targets go in on their exec session (submitting a session earlier fills a day before the decision and turned +17.5% into -5.8%). Its curve is marked before that day's fills, so expect a gap of up to ~1 pp.
- **backtrader** on 30-minute bars is subsampled to the 50 largest-weight names plus SPY (the report says so).
- `--scores-file` / `--bars-file` take parquet instead of scoring the holdout / calling Alpaca, so the engines can be exercised without keys.

## How it works
- **One trade a day.** Features are daily and the target is a 5-day return, so re-scoring every 30 minutes returns the same ranking. `snapshot_interval_min` (default 30) only sets how often equity is marked.
- **Book.** Each day's decile-10 long / decile-1 short book is one vintage. Target = average of the last `horizon` (5) vintages, so capital ramps in at 1/5 a day, as in `inference replay`. `long_only: true` drops the short leg.
- **Orders.** Whole-share market orders, `client_order_id = {asof}-{symbol}`, so a repeat run is a no-op.
- **Backtest.** Same code, `SimBroker` fills at the open bar of the session after each decision (past `execution_time`), minus `cost_bps`; bars come from Alpaca historical data (IEX feed, cached under `data/paper/bars/`).

## Modules

| Module | Job |
|---|---|
| `cli.py` | `rebalance`, `snapshot`, `report`, `backtest`, `crosscheck` |
| `config.py` | `PaperConfig`, loaded from one YAML; secrets are env-var *names* (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`) |
| `book.py` | Pure pandas: decisions → daily book → target weights → orders. No broker, no I/O |
| `broker.py` | `AlpacaBroker` (live paper) and `SimBroker` (backtest). `rebalance` and `backtest` share `book.py` and `report.py`, so a backtest exercises the same order maths as a live run |
| `live.py` | `rebalance` (score, move toward the target), `snapshot` (mark to market), `watch` (loop) |
| `ledger.py` | Audit trail of decisions, orders and snapshots. SQLAlchemy URL from config: SQLite (`data/paper/ledger.db`) by default, Postgres works unchanged. Alpaca stays the source of truth for fills; the ledger records what we asked for |
| `report.py` | Equity curve → weekly / monthly / quarterly / yearly P&L tables and summary |
| `backtest.py` | Replays the holdout through `SimBroker` on Alpaca historical bars |
| `backtesting/` | `runner`, `compare`, `inputs`, `output`, plus one adapter per engine |
| `symbols.py` | Ticker spelling: the warehouse (Yahoo) writes `BF-B`, Alpaca `BF.B` |

## Config keys (`default.yaml`)

`capital` (100000), `long_only` (false = dollar-neutral), `leg_exposure` (0.5 per leg), `horizon` (5 daily books
averaged), `rebalance_every_n_days`, `snapshot_interval_min` (30, marks only), `min_order_usd` (50), `cost_bps`
(5, SimBroker only), `execution_time` (09:35 New York), `bar_interval` (30Min), `benchmark` (SPY),
`report_periods`, `model_version` (`latest-narrow`) and `model_config`, `ledger_url`, `out_dir`, the two Alpaca
env-var names, and `feed` (`iex`). Only `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are read from the
environment; no base-URL variable is used.

## Engines

| Engine | Runs | Notes |
|---|---|---|
| sim (`SimBroker`) | in process | the reference; fills at the open bar of the submit session, minus `cost_bps` |
| vectorbt | in process (`backtesting` group) | |
| backtrader | in process (`backtesting` group) | on 30-minute bars: 50 largest-weight names plus SPY |
| Lumibot | child process, not in any dependency group | daily bars only; see above |

## Safety and limits

- Paper account only; there is no live-money code path.
- Idempotent orders (`client_order_id = {asof}-{symbol}`) so a re-run the same day sends nothing new.
- `rebalance --dry-run` shows the orders and sends none; `--force` re-runs a day already traded.
- The model's `latest-narrow` symlink must exist (it is created by the narrowed train run).
- No reconciliation job, kill switch or risk limits beyond `min_order_usd` and `leg_exposure`. Do not extend this
  to real money without adding them.

## Caveats
- Holdout IC is 0.007; short-period P&L is noise. Judge on months, not weeks.
- IEX volume is a small share of the market; prices are fine, depth is not. No borrow cost or impact in the sim.
- Universe is today's S&P 500 (survivorship), as in the rest of the repo.
