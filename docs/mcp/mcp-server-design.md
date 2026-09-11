# MCP server — a read-only SQL window onto the gold marts

> **Design — not yet built.** `src/mcp/` is an empty `__init__.py`.
> This doc is the specification the code will be reviewed against.
>
> **Supersedes for Postgres:** [`architecture/TECHNICAL_SPEC.md`](../architecture/TECHNICAL_SPEC.md) §3.9, which
> targets Snowflake.
> **Companions:** [`warehouse/data-dictionary.md`](../warehouse/data-dictionary.md) ·
> [`warehouse/rationale/gold-models-rationale.md`](../warehouse/rationale/gold-models-rationale.md) ·
> [`infra/aws-deployment-plan.md`](../infra/aws-deployment-plan.md)
>
> Written for someone who knows data engineering but has not built an MCP server before.
> Reference implementation studied: [`ColeMurray/aws-athena-mcp`](https://github.com/ColeMurray/aws-athena-mcp).

---

Contents:

1. [What this is, and what changes from the spec](#1-what-this-is-and-what-changes-from-the-spec)
2. [Architecture](#2-architecture)
3. [The catalog](#3-the-catalog)
4. [Tools](#4-tools)
5. [Guardrails — three layers](#5-guardrails--three-layers)
6. [Configuration](#6-configuration)
7. [Operational notes](#7-operational-notes)
8. [Tests](#8-tests)
9. [What is deliberately not built](#9-what-is-deliberately-not-built)
10. [Verification](#10-verification)
11. [Risks](#11-risks)
12. [See also](#12-see-also)

---

## 1. What this is, and what changes from the spec

An **MCP server** is a small local process that speaks JSON-RPC over stdin/stdout to an MCP client
— Claude Code, Claude Desktop — and advertises a set of *tools* the client may call. Here the tools
are "tell me what tables exist" and "run this SELECT". The client is the thing holding the
conversation with the user; the server is a typed, guarded door onto the warehouse.

Spec §3.9 was written against a Snowflake warehouse that does not exist, and puts an LLM API key
*inside* the server so it could turn a question into SQL. Both assumptions are now wrong in a way
that makes the design simpler, not harder.

| Spec §3.9 / Athena reference says | We do | Why |
|---|---|---|
| `screen_stocks(question)` — the server calls Claude to generate SQL | The server exposes the catalog and `run_query`; **the connected client writes the SQL** | The client is already an LLM. A second model means an API key, a per-question cost, a second place to prompt-inject, and a worse result — the client has the full conversation, the server has one string |
| Snowflake `GOLD`, read-only Snowflake role | Postgres `gold` schema, a dedicated `aurum_mcp_ro` role | Snowflake is not built. The medallion runs on RDS Postgres |
| Athena: `run_query` → `get_status` → `get_result` | One synchronous `run_query` | Athena's three-step dance exists because Athena is asynchronous. Postgres is not. A synchronous call bounded by `statement_timeout` is strictly less to get wrong |
| Athena: block "dangerous patterns" by regex | Parse the SQL to an AST and check node types | A regex for `DELETE` does not stop `WITH x AS (DELETE FROM gold.mart_features RETURNING *) SELECT * FROM x`, which Postgres executes happily. See [§5](#5-guardrails--three-layers) |
| Spec §3.9 safety: "generated SQL restricted to `SELECT`" | Three independent layers, of which the SQL check is the *weakest* | Any single check is one parser bug from a write. The database role is the layer that actually holds |

**Read scope is the `gold` schema only.** That is the standing invariant — ML and MCP read only from
GOLD — and it is also the smallest useful surface: four marts, all analyst-shaped, none of them
carrying raw vendor payloads.

---

## 2. Architecture

```
Claude Code / Desktop ──stdio (JSON-RPC)──▶ src/mcp/server.py  (FastMCP)
                                                  │
                           ┌──────────────────────┼──────────────────────┐
                           ▼                      ▼                      ▼
                      catalog.py               guard.py                db.py
              information_schema + pg_catalog  sqlglot AST gate   read-only engine
              + dbt _*.yml descriptions        + schema allowlist  + READ ONLY txn
                           └──────────────────────┼──────────────────────┘
                                                  ▼
                                        Postgres  role aurum_mcp_ro
                                        GRANT SELECT on gold.* — nothing else
```

Modules to create under `src/mcp/`:

| Module | Responsibility |
|---|---|
| `server.py` | The FastMCP app: tool + resource registration, stdio entry point (`python -m src.mcp.server`) |
| `config.py` | `MCPSettings` via `pydantic-settings` (already a runtime dep), env prefix `AURUM_MCP_` |
| `db.py` | Read-only engine factory; `execute_readonly(sql, max_rows) -> QueryResult` |
| `guard.py` | `validate_select(sql, catalog) -> str` — the AST gate, the only place SQL is judged |
| `catalog.py` | Introspection, dbt-description merge, TTL cache, markdown rendering |
| `models.py` | Pydantic result models: `QueryResult`, `TableInfo`, `ColumnInfo` |
| `cli.py` | argparse, in house style: `serve`, `catalog`, `check` |

`tests/mcp/` mirrors `tests/ingestion/`.

Two existing utilities are reused rather than rewritten: `src/utils/env.py:load_env()` (the single
`.env` loader) and `src/utils/config_reader.py:read_config()` (YAML read routed through
`src/utils/paths.py:safe_path`). The engine builder is **not** shared with
`src/ingestion/datasources/storage/db.py` — that one connects as the warehouse owner, which is
exactly what this server must never do.

---

## 3. The catalog

The catalog is what lets the client write correct SQL without guessing column names. It is built by
merging two sources, and the split is the point:

**1. The live database — authoritative for shape.**
`information_schema.columns` for names, types and nullability; `pg_catalog` for primary keys and the
`pg_class.reltuples` row estimate. Filtered to the allowlisted schemas **by the server**, never by a
caller-supplied schema name.

**2. The dbt schema YAML files — authoritative for prose.**
`src/transformation/aurum_dwh/models/**/_*.yml` carry model- and column-level `description:` text
written by whoever built the model. `_gold_models.yml` alone is 290 lines of it.

Why not `target/manifest.json` / `target/catalog.json`, which dbt already generates and which would
be easier to parse? Because `target/` is gitignored (`.gitignore:76`). It is absent in a fresh clone,
absent in CI, absent in the container, and present locally only until someone cleans it. A catalog
that depends on it would work on the machine it was written on and nowhere else. The `_*.yml` files
are checked in and are the same text.

A missing description degrades to `""`. The catalog never fails because prose is missing, and where
the two sources disagree about columns, **the live database wins** — the YAML is documentation, not
a contract.

Cached in-process for `AURUM_MCP_CATALOG_TTL_S` (default 900s), with a `refresh_catalog` tool to
bust it after a dbt build. Also published as an MCP **resource**, `aurum://catalog`, rendered as
compact markdown — a resource is loaded once into the client's context rather than re-fetched per
question, which is the difference between one catalog read and twenty.

What it covers today:

| Table | Grain | Note |
|---|---|---|
| `gold.mart_features` | (symbol, date) | The feature store, ~2.9M rows, 503 symbols, 2000→today. No target columns by construction. **Unbounded selects here matter** |
| `gold.mart_training_set` | (symbol, date) | Features + targets + walk-forward fold ids |
| `gold.mart_feature_summary` | (feature) | Per-feature coverage and distribution |
| `gold.mart_stock_screener` | (symbol, date) | The analyst-facing joined mart — spec line 272 names it the MCP query target |

---

## 4. Tools

| Tool | Params | Returns |
|---|---|---|
| `list_tables` | `schema="gold"` | name, kind, row estimate, description |
| `describe_table` | `table`, `schema="gold"` | columns (name, type, nullable, description), primary key, row estimate |
| `get_catalog` | — | the whole allowlisted catalog as markdown |
| `refresh_catalog` | — | busts the TTL cache; returns the new table count |
| `run_query` | `sql`, `max_rows=1000` | `columns`, `rows`, `row_count`, `truncated`, `elapsed_ms`, `executed_sql` |
| `explain_query` | `sql` | the planner's cost and row estimate — `EXPLAIN` **without** `ANALYZE`, which would execute the query |
| `sample_table` | `table`, `schema="gold"`, `n<=100` | rows, built from validated identifiers with no caller string reaching the SQL |

`run_query` returns `executed_sql` on purpose. The server rewrites the query to bound it
([§5.3](#53-layer-3--the-sql-ast-gate)), and a caller that cannot see the rewrite cannot tell a
truncated answer from a complete one.

`truncated` is a flag, not a guess: the server asks for `max_rows + 1` rows and reports `truncated`
when it gets them, then returns `max_rows`.

**Not now:** the three convenience tools from spec §3.9 — `get_stock_fundamentals(ticker)`,
`compare_peers(tickers)`, `sector_performance(sector)`. They are worth building once the catalog
+ `run_query` pair has been used in anger and the common questions are known. When they are built,
every ticker argument is a **bound parameter**, never an f-string.

---

## 5. Guardrails — three layers

The requirement is that this server cannot alter the database. One check is not enough, because one
check is one parser bug away from being no check. Three independent layers, listed weakest-last:

| Layer | Mechanism | What it alone would stop | What gets through if *only* this layer is removed |
|---|---|---|---|
| 1 | Postgres role grants | Everything — writes, DDL, reads outside `gold` | A SQL-parser bypass becomes a real write. **This is the layer that actually holds** |
| 2 | `READ ONLY` transaction + timeouts | Writes, runaway queries, idle transactions holding locks | A write that the AST gate mis-classified; a query that pins a connection forever |
| 3 | sqlglot AST gate | Non-SELECT statements, cross-schema reads, unbounded result sets | A clear error becomes an opaque `permission denied`, and a 2.9M-row result set reaches the client |

### 5.1 Layer 1 — the Postgres role

A dedicated login role that is incapable of writing, shipped as `infra/sql/mcp_readonly_role.sql`
and applied once by hand with `psql`:

```sql
CREATE ROLE aurum_mcp_ro LOGIN PASSWORD :'pw'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

ALTER ROLE aurum_mcp_ro SET default_transaction_read_only        = on;
ALTER ROLE aurum_mcp_ro SET statement_timeout                    = '30s';
ALTER ROLE aurum_mcp_ro SET idle_in_transaction_session_timeout  = '60s';

GRANT CONNECT ON DATABASE aurum TO aurum_mcp_ro;
GRANT USAGE   ON SCHEMA   gold  TO aurum_mcp_ro;
GRANT SELECT  ON ALL TABLES IN SCHEMA gold TO aurum_mcp_ro;

-- LOAD-BEARING. dbt materializes the gold marts as `table`, which DROPs and
-- recreates them on every build. Table-level grants die with the old table, so
-- without this line the server works until the next `dbt build` and then starts
-- returning "permission denied for table mart_features" with nothing in the
-- diff to explain it. Default privileges attach the grant to objects that do
-- not exist yet.
ALTER DEFAULT PRIVILEGES FOR ROLE <dbt_owner> IN SCHEMA gold
  GRANT SELECT ON TABLES TO aurum_mcp_ro;

REVOKE ALL ON SCHEMA public, bronze, silver FROM aurum_mcp_ro;
```

`<dbt_owner>` is whichever role `dbt build` runs as — today the value of `AURUM_USERNAME`. The
default-privileges grant is per-granting-role, so it must name that role, not the MCP role.

The password goes into SSM Parameter Store as a `SecureString` (same pattern as the existing
`AURUM_USERNAME` / `AURUM_PASSWORD` params in `tasks.tf`) and into the local `.env` as
`AURUM_MCP_PASSWORD`.

*Rejected:* reuse `AURUM_USERNAME`. It owns the warehouse. Every other layer would then be the only
thing standing between a parser bug and a dropped mart.

*Rejected for now:* managing the role in Terraform via the `cyrilgdn/postgresql` provider. It would
need the provider to reach RDS from wherever `apply` runs, and `apply` runs in GitHub Actions with
no route into the VPC. A checked-in SQL file plus a runbook line is honest about what actually
happens.

### 5.2 Layer 2 — connection and transaction

```python
create_engine(
    url,
    pool_size=1, max_overflow=2, pool_pre_ping=True,
    connect_args={
        "sslmode": "require",
        "application_name": "aurum-mcp",
        "options": (
            "-c default_transaction_read_only=on "
            "-c statement_timeout=30000 "
            "-c idle_in_transaction_session_timeout=60000"
        ),
    },
)
```

Every query runs inside an explicit transaction opened with `SET TRANSACTION READ ONLY` and closed
with `rollback()`. **The code contains no call to `commit()`** — that is a thing to grep for in
review, and a thing `tests/mcp/test_db.py` asserts.

`application_name` is set so a hung query is identifiable in `pg_stat_activity` without guessing.

### 5.3 Layer 3 — the SQL AST gate

`guard.validate_select(sql, catalog) -> str` runs before a single character reaches the driver. It
parses with `sqlglot` (Postgres dialect) and judges **node types**, not keywords. It fails closed:
anything that does not parse is rejected.

1. **Length cap** — `AURUM_MCP_MAX_SQL_CHARS`, default 20000.
2. **Exactly one statement.** `sqlglot.parse()` must return a list of length 1. Kills `;`-chaining
   and every comment trick built on it.
3. **Root node must be** `Select`, `Union`, `Intersect`, `Except`, or a `With` whose body is one of
   those. Everything else is rejected by type.
4. **Walk the whole tree** and reject any `Insert`, `Update`, `Delete`, `Merge`, `Create`, `Drop`,
   `Alter`, `Grant`, `Truncate`, `Copy`, `Command`, `Into` (`SELECT … INTO t`) or `Lock`
   (`FOR UPDATE` / `FOR SHARE`, which take row locks).

   **The CTE-DML case is why this is an AST walk and not a root-node check:**

   ```sql
   WITH gone AS (DELETE FROM gold.mart_features RETURNING *) SELECT count(*) FROM gone;
   ```

   Postgres executes this. Its root node is a `With` wrapping a `Select`, so step 3 passes it. A
   regex looking for a leading `DELETE` passes it too. Only the walk catches it.

5. **Function deny-list** — `pg_read_file`, `pg_read_binary_file`, `pg_ls_dir`, `lo_import`,
   `lo_export`, `dblink*`, `pg_sleep`, `pg_terminate_backend`, `set_config`, `query_to_xml`.
   Matched on `exp.Anonymous` names, case-folded.
6. **Schema allowlist.** Collect every `exp.Table` in the tree, subtract the CTE aliases (a CTE name
   is not a table), qualify bare names with the single default schema (`gold`), and require every
   survivor to be both in `AURUM_MCP_ALLOWED_SCHEMAS` *and* present in the live catalog. This is what
   blocks `bronze.*`, `silver.*`, `public.*`, `pg_catalog.*` and `information_schema.*`. The catalog
   tools do not pass through this gate — they run fixed, hand-written queries.
7. **Bound the result set by wrapping, not appending:**

   ```sql
   SELECT * FROM ( <caller sql> ) AS _mcp_q LIMIT :n
   ```

   Appending ` LIMIT n` to the caller's text breaks on `UNION`, on a trailing `ORDER BY`, and on a
   query that already has a `LIMIT`. Wrapping survives all three. `:n` is `max_rows + 1`.

---

## 6. Configuration

Environment only — no YAML. There is nothing per-run to configure, so the ingestion framework's
config-file pattern would be ceremony.

| Variable | Default | Purpose |
|---|---|---|
| `HOST`, `PORT` | — | Reused from the existing `.env`, same names as ingestion and dbt |
| `AURUM_MCP_DB_NAME` | `aurum` | |
| `AURUM_MCP_USERNAME` | — | The read-only role. **Not** `AURUM_USERNAME` |
| `AURUM_MCP_PASSWORD` | — | From SSM / `.env` |
| `AURUM_MCP_ALLOWED_SCHEMAS` | `gold` | Comma-separated. Widening this is a deliberate act |
| `AURUM_MCP_MAX_ROWS` | `1000` | Hard ceiling; the tool's `max_rows` argument is clamped to it |
| `AURUM_MCP_STATEMENT_TIMEOUT_MS` | `30000` | Mirrors the role-level setting |
| `AURUM_MCP_CATALOG_TTL_S` | `900` | |
| `AURUM_MCP_MAX_SQL_CHARS` | `20000` | |

Read with `pydantic-settings` (already a runtime dependency) layered on `src/utils/env.load_env()`.
Never `load_dotenv` an absolute path — that rule is in `CLAUDE.md` and it holds here.

Note the deliberate break from the ingestion convention where a config value is an env var *name*
(`username: "AURUM_USERNAME"`). That indirection exists so one YAML can name different secrets per
feed. There is one MCP server and one credential, so the values are read directly.

**New `mcp` dependency group**, not `[project].dependencies` — `src/ingestion` and `main.py` never
import it, which is exactly the rule `CLAUDE.md` states:

```toml
mcp = [
    "mcp[cli]>=1.2",
    "sqlglot>=25",
]
```

Both are pure-Python wheels, so CI's `uv sync --locked --no-build` install path is unaffected. CI
does **not** need the group unless `tests/mcp/` is added to the pytest run — and it should be, so
the CI install step gains `--group mcp`.

---

## 7. Operational notes

**stdout is the protocol.** Under the stdio transport, the JSON-RPC framing owns stdout. A stray
`print()`, a library that writes a banner, or `logging.basicConfig()` with its default stream will
corrupt the stream and the client will drop the connection with a parse error that names nothing
useful. Every log goes to stderr:

```python
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
```

This is the most likely first bug. Check it first.

**Audit line per query**, at INFO on stderr: sha256-prefix of the SQL, row count, elapsed ms,
status, truncated flag. The connection string is never logged.

**Errors are redacted.** Driver exceptions can carry the DSN, and the DSN carries the password.
`db.py` maps `SQLAlchemyError` to a clean message plus the Postgres `SQLSTATE` and returns that;
the raw exception text goes to the stderr log, not to the client.

**Client wiring** — an entry in `.mcp.json` (which today registers only the external `pycharm`
server):

```json
"aurum": {
  "command": "uv",
  "args": ["run", "--group", "mcp", "python", "-m", "src.mcp.server"]
}
```

---

## 8. Tests

`tests/mcp/`, following the existing conventions: `monkeypatch` only (no `unittest.mock`),
`tmp_path` for files, and **no live database** — see `tests/ingestion/test_postgres_datasource.py`
for the pattern of monkeypatching the read at the module object and capturing the emitted SQL.

| File | Covers |
|---|---|
| `test_guard.py` | The hostile-SQL table below, parametrized — every case must raise. Plus a benign table that must all pass: plain `SELECT`, CTEs, `UNION ALL`, window functions, an existing `LIMIT` |
| `test_catalog.py` | Merging a fixture `_gold_models.yml` with a canned `information_schema` frame; the missing-description path; live-database-wins on a column the YAML does not mention |
| `test_db.py` | The read-only transaction is opened; `commit()` is never called; the `LIMIT` wrapper has the expected shape and asks for `max_rows + 1` |
| `test_server.py` | Tool registration, `max_rows` clamping to `AURUM_MCP_MAX_ROWS`, error redaction (no password in any returned string) |

The hostile-SQL table — each row is a real bypass, not a hypothetical:

```
DROP TABLE gold.mart_features
DELETE FROM gold.mart_features
UPDATE gold.mart_features SET close = 0
SELECT 1; DROP TABLE gold.mart_features
WITH gone AS (DELETE FROM gold.mart_features RETURNING *) SELECT * FROM gone
SELECT * FROM gold.mart_features FOR UPDATE
SELECT * INTO scratch FROM gold.mart_features
COPY gold.mart_features TO PROGRAM 'sh -c ...'
SELECT pg_read_file('/etc/passwd')
SELECT * FROM bronze.br_ohlcv_1d
SELECT * FROM information_schema.tables
SELECT * FROM pg_catalog.pg_shadow
```

---

## 9. What is deliberately not built

**Not included:** HTTP/SSE transport, `aws_ecs_service`, an ALB, any security-group ingress rule,
any Terraform. A long-lived listening service is a genuinely new shape for this infrastructure —
`infra/terraform/` today has zero `aws_ecs_service` resources, zero load balancers, and a tasks
security group with **no ingress rules at all**. Standing that up is a separate piece of work with
a separate authentication question, and it is not needed to answer "which S&P 500 stocks had the
highest Sharpe ratio last month" from a laptop.

`infra/aws-deployment-plan.md` lists `src/mcp/` under "Not included". This doc supersedes that
non-goal **for local stdio only**; the deployment non-goal stands.

Also not built: any LLM API key inside the server; any write path, including a scratch schema; the
three §3.9 convenience tools; read access to `bronze` or `silver`.

---

## 10. Verification

```bash
# 1. Guardrail self-test — runs the hostile-SQL table through guard.validate_select
#    with no database. Must print PASS for every case and exit 0.
uv run --group mcp python -m src.mcp.cli check

# 2. Catalog against the live database. Must list exactly the four gold marts,
#    with column counts and descriptions, and no bronze/silver/public table.
uv run --group mcp python -m src.mcp.cli catalog

# 3. The role genuinely cannot write. Run as aurum_mcp_ro:
psql "$MCP_DSN" -c 'CREATE TABLE gold.x(i int);'             # must fail, SQLSTATE 25006
psql "$MCP_DSN" -c 'DELETE FROM gold.mart_features;'         # must fail, SQLSTATE 25006
psql "$MCP_DSN" -c 'SELECT count(*) FROM bronze.br_ohlcv_1d;'# must fail, SQLSTATE 42501
psql "$MCP_DSN" -c 'SELECT count(*) FROM gold.mart_features;'# must succeed, ~2.9M

# 4. The grant survives a dbt rebuild — the ALTER DEFAULT PRIVILEGES check.
cd src/transformation/aurum_dwh && uv run --group dbt dbt run --select mart_features
psql "$MCP_DSN" -c 'SELECT count(*) FROM gold.mart_features;'# must STILL succeed

# 5. The server speaks MCP and the tools are listed.
uv run --group mcp mcp dev src/mcp/server.py

# 6. The gates in CI.
uv run ruff check src/ main.py
uv run pytest tests/mcp/ -v
```

---

## 11. Risks

| # | Risk | Cost | Mitigation |
|---|---|---|---|
| 1 | `dbt build` drops and recreates the gold marts, taking the table grants with them | Server returns `permission denied` with nothing in any diff to explain it | `ALTER DEFAULT PRIVILEGES` ([§5.1](#51-layer-1--the-postgres-role)); verification step 4 exercises it |
| 2 | sqlglot mis-parses a Postgres construct | A legitimate query is rejected | Fail-closed is the correct direction. The user rephrases; `explain_query` tells them the query was never run |
| 3 | An expensive aggregate over 2.9M rows pins a connection | Slow tool call, one connection held | `statement_timeout` at both role and session level; `explain_query` exists so cost can be checked before running |
| 4 | The dbt YAML drifts from the live table | A described column no longer exists | Live database wins for shape; YAML supplies prose only. Drift shows as a column with no description, which is visible |
| 5 | The client writes SQL that is valid, permitted, and wrong | A confidently wrong answer | Out of the server's scope by construction. `executed_sql` and `row_count` are returned so the answer is auditable |
| 6 | `AURUM_MCP_ALLOWED_SCHEMAS` is widened casually | Raw landing tables reachable | It is one env var and a one-line diff — review it like a grant, because it is one |

---

## 12. See also

| Doc | Content |
|---|---|
| [`architecture/TECHNICAL_SPEC.md`](../architecture/TECHNICAL_SPEC.md) | §3.9 — the original Snowflake-targeted design this doc replaces for Postgres |
| [`warehouse/data-dictionary.md`](../warehouse/data-dictionary.md) | Every field in every layer — what the catalog is describing |
| [`warehouse/dwh-medallion.md`](../warehouse/dwh-medallion.md) | The medallion as built; where the gold marts come from |
| [`warehouse/rationale/gold-models-rationale.md`](../warehouse/rationale/gold-models-rationale.md) | Why each mart is shaped the way it is |
| [`infra/aws-deployment-plan.md`](../infra/aws-deployment-plan.md) | The deployed system, and the non-goal this doc partially supersedes |
| [`operations/cicd.md`](../operations/cicd.md) | The lint/test/Sonar gates the new package must pass |
