# MCP server — a read-only SQL window onto the gold marts

> **As built.** Local stdio server in `src/mcp/`, modelled on
> [`ColeMurray/aws-athena-mcp`](https://github.com/ColeMurray/aws-athena-mcp), over Postgres `gold`.
> The earlier, heavier design (sqlglot AST gate, TTL cache, seven tools) was dropped as more machinery than
> the database's own guarantees need.

## Shape

| Module | Job |
|---|---|
| `src/mcp/server.py` | `MCPServer` app: four tools and the `aurum://catalog` resource. `python -m src.mcp.server` |
| `src/mcp/db.py` | Read-only engine, `run_select()`, first-keyword check, env config |
| `src/mcp/catalog.py` | In-memory table catalog: dbt docs files, live fallback |

## Tools

| Tool | Returns |
|---|---|
| `list_tables(schema="gold")` | table, description, column count |
| `describe_table(table, schema="gold")` | columns (name, type, description); live lookup on a cache miss |
| `run_query(sql, max_rows=1000)` | columns, rows, row_count, truncated, elapsed_ms |
| `refresh_catalog()` | reloads the catalog after a dbt build |

The reference's `get_status` / `get_result` do not exist: Athena is asynchronous, Postgres is not, and
`statement_timeout` replaces the poll loop. The connected client writes the SQL; there is no LLM in the server.

## Catalog

Loaded once, on first use, and held in memory. Types come from `target/catalog.json`, descriptions from
`target/manifest.json`. `target/` is gitignored, so on a fresh clone, in CI or in the container the files are
absent: the catalog is then filled by one `information_schema.columns` query. A table the files do not know is
looked up the same way and cached (Postgres has no `DESCRIBE`). Run `refresh_catalog` after `dbt build` /
`dbt docs generate`.

## Safety — Postgres does the work

1. Every session opens with `default_transaction_read_only=on` and `statement_timeout` (default 30 s).
2. Each query runs in `SET TRANSACTION READ ONLY` and is rolled back. `db.py` contains no `commit()`; a test asserts it.
3. `check_select` accepts one statement starting with `select|with|explain|show`. It exists for readable errors,
   not as the guard: `WITH g AS (DELETE …) SELECT …` passes it and is stopped by the read-only transaction
   (SQLSTATE 25006).
4. Reads are limited to `AURUM_MCP_SCHEMAS` (default `gold`) for the catalog tools. **Schema isolation for
   `run_query` comes from the role**: apply `infra/sql/mcp_readonly_role.sql` and set `AURUM_MCP_USERNAME`.
   Without it the server falls back to `AURUM_USERNAME` (the warehouse owner, still read-only) and logs a warning.

Rows are fetched `max_rows + 1` over a server-side cursor, so `truncated` is a fact and a 2.9M-row mart is never
pulled into memory. Errors return a clean message plus SQLSTATE; raw driver text (which can carry the DSN) goes
to stderr only. stdout is the JSON-RPC stream, so nothing else may print to it.

## Configuration

`HOST`, `PORT` (shared with ingestion), plus `AURUM_MCP_USERNAME`, `AURUM_MCP_PASSWORD`, `AURUM_MCP_DB_NAME`
(`aurum`), `AURUM_MCP_SCHEMAS` (`gold`), `AURUM_MCP_MAX_ROWS` (`1000`), `AURUM_MCP_TIMEOUT_MS` (`30000`),
`AURUM_MCP_DBT_TARGET` (the dbt `target/` dir).

Transport: `AURUM_MCP_TRANSPORT` (`stdio` default, or `streamable-http`), `AURUM_MCP_LISTEN_HOST` (`127.0.0.1`),
`AURUM_MCP_LISTEN_PORT` (`8000`). `HOST`/`PORT` are the Postgres endpoint, hence the separate names. The HTTP
transport has **no authentication**, so `listen_config()` refuses a non-loopback host unless
`AURUM_MCP_ALLOW_PUBLIC_BIND=1`. In HTTP mode `main()` warms the catalog before serving; a database that is down
at boot logs a warning instead of stopping the server.

## Run

```bash
uv sync --group mcp
uv run --group mcp mcp dev src/mcp/server.py        # inspector
uv run --group mcp pytest tests/mcp -v
```

`.mcp.json` registers it as `aurum` for Claude Code. Not built: convenience tools (`compare_peers` etc.).

## Hosting on EC2

One `t3.micro` runs the slim image (`docker build --target mcp -f docker/aurum.Dockerfile .`, tagged
`mcp-<sha>` in the same immutable ECR repo by `terraform.yml`). Terraform is `infra/terraform/mcp_host.tf`; set
`mcp_enabled = false` to tear it down.

- **Private by construction.** The server binds the instance's loopback, the security group has no ingress rules,
  and the only way in is an SSM Session Manager port-forward (IAM-gated; no key pair, domain or load balancer):
  `terraform output mcp_port_forward_command`, then point `.mcp.json` at
  `{"aurum": {"type": "http", "url": "http://127.0.0.1:8000/mcp"}}`. Needs the AWS CLI and the Session Manager
  plugin locally. claude.ai connectors cannot use this; that would need a public hostname, TLS and a token verifier.
- **Database access.** The host's security group is added to the 5432 rule on `aws_security_group.data`. It connects
  as the read-only role, from SSM parameters `/aurum/mcp_username` and `/aurum/mcp_password` read at service start,
  so rotating the password is a parameter change plus `systemctl restart aurum-mcp` (via a session).
- **Catalog.** No dbt artifacts on the host: the catalog is one live `information_schema` query, held in memory
  (no disk cache). Names and types only, no descriptions. `refresh_catalog` reloads it after a `dbt build`.
- **Rollout.** A new `mcp-<sha>` tag changes `user_data`, which replaces the instance (a couple of minutes of
  downtime). The ECR lifecycle policy keeps the last three `mcp-` images separately from the ECS images.

### Setup guide

Placeholders used below. Substitute them; never commit real values.

| Placeholder | Meaning |
|---|---|
| `<MCP_LOGIN_PASS>` | Password of the `aurum_mcp_ro` role. **One value, set in three places** (step 1, step 2 twice). |
| `<OWNER_ROLE>` | The role that runs dbt and owns the `gold`/`silver` tables (`aurum` here). Default privileges only fire for tables *this* role creates. |
| `<RDS_HOST>` | RDS endpoint, e.g. `aurum.<id>.us-east-1.rds.amazonaws.com`. |

**1. Create the role.** Connect to database `aurum` **as `<OWNER_ROLE>`** (psql or any SQL client) and run:

```sql
CREATE ROLE aurum_mcp_ro LOGIN PASSWORD '<MCP_LOGIN_PASS>' NOSUPERUSER NOCREATEDB NOCREATEROLE;

ALTER ROLE aurum_mcp_ro SET default_transaction_read_only = on;
ALTER ROLE aurum_mcp_ro SET statement_timeout = '30s';

GRANT CONNECT ON DATABASE aurum TO aurum_mcp_ro;
GRANT USAGE  ON SCHEMA gold, silver TO aurum_mcp_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA gold, silver TO aurum_mcp_ro;

-- Load-bearing: dbt rebuilds gold marts and silver models as DROP + CREATE, which discards
-- table-level grants. Default privileges attach SELECT to tables that do not exist yet.
ALTER DEFAULT PRIVILEGES FOR ROLE <OWNER_ROLE> IN SCHEMA gold, silver GRANT SELECT ON TABLES TO aurum_mcp_ro;

REVOKE ALL ON SCHEMA public, bronze FROM aurum_mcp_ro;
```

The same SQL is `infra/sql/mcp_readonly_role.sql`; from a shell:
`psql -h <RDS_HOST> -d aurum -U <OWNER_ROLE> -v pw="'<MCP_LOGIN_PASS>'" -v owner=<OWNER_ROLE> -f infra/sql/mcp_readonly_role.sql`.
Run it once. The role survives `dbt build`, but a `terraform destroy` that recreates RDS wipes it, so redo this step
after any rebuild. The schemas need not exist yet (`GRANT USAGE ON SCHEMA` does, so run `dbt build` first on a fresh DB).

**2. Give the same password to Terraform.** Set `mcp_password = "<MCP_LOGIN_PASS>"` in `terraform.tfvars` (gitignored)
and the repository secret `TF_VAR_MCP_PASSWORD`. Terraform writes it to SSM `/aurum/mcp_password` (SecureString);
the host reads it at service start.

**3. Deploy.** Merge to `main`; `apply` builds both images and creates the host. Check it registered:
`aws ssm describe-instance-information`.

**4. Connect.** Open the port-forward (`scripts/mcp-tunnel.sh`, or `terraform output mcp_port_forward_command`) and
start Claude Code; `.mcp.json` points at `http://127.0.0.1:8000/mcp`.

**5. Verify.** Call `list_tables` for `silver` and `gold`. Expect 8 silver and 4 gold tables once dbt has built
gold. An empty `gold` list with a working connection means gold has not been built, not a grant problem.

**Rotate or repair the password.** The role and SSM must match, or every call fails with
`password authentication failed for user "aurum_mcp_ro"`. As `<OWNER_ROLE>`:

```sql
ALTER ROLE aurum_mcp_ro PASSWORD '<MCP_LOGIN_PASS>';
```

If the SSM value is also changing: update `terraform.tfvars` and `TF_VAR_MCP_PASSWORD`, `terraform apply`, then
`sudo systemctl restart aurum-mcp` on the host (SSM session). If only the role was wrong, the `ALTER ROLE` alone is
enough: Postgres checks the password on each new connection and the container needs no restart.

| Symptom | Cause |
|---|---|
| `password authentication failed for user "aurum_mcp_ro"` | Role password differs from SSM `/aurum/mcp_password`. `ALTER ROLE` as above. |
| `role "aurum_mcp_ro" does not exist` | Role never created, or RDS was rebuilt. Redo step 1. |
| `list_tables` returns `[]` for gold | dbt gold not built, or built by a role other than `<OWNER_ROLE>`. |
| `permission denied for schema gold` | Step 1 grants missing; rerun them. |
| Tunnel returns HTTP 400 | Port-forward is up but the host container is not; check `systemctl status aurum-mcp`. |

The role grants `gold` and `silver` (matching `mcp_schemas = "gold,silver"`); public and bronze are revoked.
Silver holds the typed `stg_*` staging and `int_*` feature models behind the gold marts, so it serves as the
intermediate-step check next to them.
