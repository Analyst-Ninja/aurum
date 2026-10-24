# Local setup: from an empty machine to a backtest

Everything runs in Docker, including Postgres, so nothing is installed on the host beyond Docker itself. This walks the whole pipeline in order: ingestion → dbt → train → SHAP → narrow retrain → model backtest → decisions → paper-trading backtest.

Related: [`training-container.md`](training-container.md) (image and compose internals), [`../modeling/pipeline-runbook.md`](../modeling/pipeline-runbook.md) (what each modelling step means), [`../paper/paper-trading.md`](../paper/paper-trading.md).

## 0. Prerequisites

- Docker Desktop with **8 GB or more** of memory. The first Yahoo load pulls 503 symbols × 26 years and OOMs below that.
- A repo-root `.env`:

```bash
SEC_USER_AGENT="Your Name you@example.com"   # EDGAR returns 403 without it
HOST=localhost
PORT=5432
AURUM_USERNAME=...
AURUM_PASSWORD=...
ALPACA_API_KEY=...                           # step 8 only (paper account)
ALPACA_SECRET_KEY=...
```

Compose overrides `HOST` to `host.docker.internal`, so `.env` keeps `localhost` for host-side tools.

## 1. Postgres

```bash
docker run -d --name aurum-pg -p 5432:5432 \
  -e POSTGRES_USER=$(grep ^AURUM_USERNAME .env | cut -d= -f2) \
  -e POSTGRES_PASSWORD=$(grep ^AURUM_PASSWORD .env | cut -d= -f2) \
  -e POSTGRES_DB=aurum \
  -v aurum-pgdata:/var/lib/postgresql/data postgres:16
```

## 2. Build the image and set up the shell

```bash
export AURUM_GIT_SHA=$(git rev-parse HEAD)   # the image has no .git; this stamps the model version
# creating alias A to avoid rewriting common part of the command
A='docker compose -f docker-compose.modeling.yml run --rm trainer'
docker compose -f docker-compose.modeling.yml build trainer
A dbt debug                                  # smoke test: reaches Postgres
```

Keep the `export` and `alias` in the shell you use for the remaining steps. The first argument after `A` picks the workload: `ingest`, `dbt`, `model` or `infer`.

## 3. Ingest

Market data, full history:

```bash
A ingest -c src/ingestion/configs/yahoo/ohlcv_1d.yaml -f True
```

EDGAR, six configs. **Truncate before each load**: the sink appends and does not deduplicate, so a rerun without truncating duplicates ~1.9M rows. Run prices first, because silver joins prices and fundamentals.

```bash
for c in income_statements_quarterly income_statements_yearly \
         balance_sheet_statements_quarterly balance_sheet_statements_yearly \
         cashflow_statements_quarterly cashflow_statements_yearly; do
  A python -m src.ingestion.truncate -c src/ingestion/configs/edgar/$c.yaml
  A ingest -c src/ingestion/configs/edgar/$c.yaml
done
```

## 4. Warehouse

```bash
A dbt seed
A dbt build        # bronze → silver → gold, with all dbt tests
```

## 5. Train, evaluate, rank features

```bash
CFG=src/modeling/configs/lgbm_xs_excess_5d.yaml
A model train -c $CFG
V=$(ls -t models | grep -v latest | head -1); echo $V
A model evaluate        -c $CFG --version $V
A model select-features -c $CFG --version $V
```

## 6. Narrow retrain

Push the SHAP ranking into the warehouse, retrain on the selected features, then compare against the full model.

```bash
A dbt seed  --select selected_features
A dbt build --select mart_feature_summary
NCFG=src/modeling/configs/lgbm_xs_excess_5d_narrow.yaml
A model train    -c $NCFG
NV=$(ls -t models | grep -v latest | head -1); echo $NV
A model evaluate -c $NCFG --version $NV
A model compare  -c $NCFG --version $NV --baseline $V
```

## 7. Model backtest and decisions

```bash
A model backtest -c $NCFG --version $NV   # writes backtest/report.html under models/<version>/
A infer decide --version $NV               # BUY/SELL/HOLD from the narrowed run
A infer replay --version $NV               # backtest that rule against a momentum baseline
```

## 8. Paper-trading backtest and cross-check

The stock image has no `paper` or `backtesting` dependencies, so add a layer:

```bash
docker build -t aurum:paper -f - . <<'EOF'
FROM aurum:local
RUN uv sync --locked --group dbt --group modeling --group paper --group backtesting --no-install-project
EOF

P() {
  docker run --rm --env-file .env -e HOST=host.docker.internal \
    --add-host host.docker.internal:host-gateway \
    -v "$PWD/models:/app/models" -v "$PWD/data:/app/data" -v "$PWD/results:/app/results" \
    aurum:paper "$@"
}

P python -m src.paper.cli backtest   --start 2024-01-01 --end 2026-10-01 --model-version $NV --bar-interval 1Day -p W,M,Q,Y
P python -m src.paper.cli crosscheck --start 2024-01-01 --end 2026-10-01 --model-version $NV --bar-interval 1Day
```

- `--model-version` takes a registry version. `latest-narrow` is a symlink that exists only on the trainer host, not locally, so the CLI default fails with "No run 'latest-narrow' under models". Always pass `$NV` (or `latest`).
- Bars come from Alpaca, so the `ALPACA_*` keys are required unless you pass `--bars-file`.
- **Use `--bar-interval 1Day` for multi-year windows.** The default `30Min` is ~9k bars per symbol over 2.7 years; the first 100-symbol batch took 13 minutes and the connection then dropped, losing everything (the cache is written only after every batch). Keep `30Min` for windows of a few weeks.
- Lumibot runs in a child process (`uv run --with lumibot --with "mcp<2"`) because it needs `mcp<2`. It downloads at run time and the container's cache is throwaway. If it fails, use `--engines sim,vectorbt,backtrader`.
- Output lands in `results/`.

## Daily refresh and reset

```bash
A ingest -c src/ingestion/configs/yahoo/ohlcv_1d.yaml -f False && A dbt build
```

Wipe the database and start over:

```bash
docker rm -f aurum-pg && docker volume rm aurum-pgdata
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Ingest exits 137 | Docker out of memory. Raise Docker Desktop memory or lower `batch_size` in the feed YAML. |
| `connection refused` to Postgres | `aurum-pg` not running, or `PORT` in `.env` is not 5432. |
| EDGAR 403 | `SEC_USER_AGENT` unset or not a real name and email. |
| Model dir named `…-unknown` | `AURUM_GIT_SHA` not exported in this shell. |
| `No run 'latest-narrow' under models` | Default `--version` is a symlink absent locally. Pass `--version $NV` to `infer` and `paper` commands. |
| `zsh: no such file or directory: docker run ...` | `P` was defined as a string variable; zsh does not word-split it. Define `P` as a function (step 8). |
| `RemoteDisconnected` after a long `Fetching bars` | 30Min bars over years. Re-run with `--bar-interval 1Day`. |
| Duplicate EDGAR rows | Skipped the truncate. Truncate and reload that config. |
| `Permission denied` writing `models/` | Container runs as uid 1000. Add `user:` to the compose service or `chown` the dir. |
