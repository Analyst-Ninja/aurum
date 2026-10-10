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

## Setup (EC2 host)

One-time, about 10 minutes. You need the AWS CLI, the
[Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html),
Node (for `npx`) and working AWS credentials.

1. **Create the read-only DB role** (as the warehouse owner):
   `psql -h <rds-host> -d aurum -v pw="'<password>'" -v owner=<owner role> -f infra/sql/mcp_readonly_role.sql`
   (`pw` carries its own single quotes; the script's `CREATE ROLE ... LOGIN` makes the role able to log in.)
2. **Give Terraform the same password**: `mcp_password` in `infra/terraform/terraform.tfvars` and the GitHub
   repository secret `TF_VAR_MCP_PASSWORD`.
3. **Deploy**: merge to `main`. `terraform.yml` builds both images and creates the instance. Check it is
   reachable: `aws ssm describe-instance-information` lists it.
4. **Connect from Claude Code**: add this to `.mcp.json` (gitignored, so it is yours). It starts the tunnel if it
   is not already running, then bridges stdio to the HTTP server:

   ```json
   {
     "mcpServers": {
       "aurum": {
         "type": "stdio",
         "command": "sh",
         "args": ["-c", "pgrep -f mcp-tunnel.sh >/dev/null || (nohup ./scripts/mcp-tunnel.sh >/tmp/aurum-mcp-tunnel.log 2>&1 &); sleep 5; exec npx -y mcp-remote http://127.0.0.1:8000/mcp --allow-http"]
       }
     }
   }
   ```

   Restart Claude Code, run `/mcp`, and ask for `list_tables`.

`scripts/mcp-tunnel.sh` finds the instance by its `Name=aurum-mcp` tag, so it survives instance replacement, and
reconnects when SSM drops an idle session. To run the tunnel by hand: `./scripts/mcp-tunnel.sh`
(`AWS_REGION` defaults to `us-east-1`, `AURUM_MCP_LOCAL_PORT` to `8000`).

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `connection refused` on `localhost:8000` | Tunnel is not running. `cat /tmp/aurum-mcp-tunnel.log`; check AWS credentials and the Session Manager plugin. |
| Password authentication failed | `mcp_password` differs from the role's. Re-run step 1's `ALTER ROLE aurum_mcp_ro PASSWORD`, then `systemctl restart aurum-mcp` in an SSM session. |
| Role exists but cannot log in | Created without `LOGIN`: `ALTER ROLE aurum_mcp_ro LOGIN;`. |
| `list_tables` is empty | Schema not in `AURUM_MCP_SCHEMAS` (`mcp_schemas` in Terraform), or the role lacks `USAGE`/`SELECT` on it. Pass `schema="silver"` to query another allowed schema. |
| `permission denied for schema bronze` | By design: bronze is revoked from the role. |

The role grants `gold` and `silver` (matching `mcp_schemas = "gold,silver"`); public and bronze are revoked.
Silver holds the typed `stg_*` staging and `int_*` feature models behind the gold marts, so it serves as the
intermediate-step check next to them. `list_tables` defaults to `gold`; pass `schema="silver"` for the rest.
