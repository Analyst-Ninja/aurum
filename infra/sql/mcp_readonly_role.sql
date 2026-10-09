-- Least-privilege login for the MCP server. Apply once, by hand, as the warehouse owner:
--   psql "$OWNER_DSN" -v pw="'<password>'" -v owner=<dbt_owner_role> -f infra/sql/mcp_readonly_role.sql
-- then set AURUM_MCP_USERNAME=aurum_mcp_ro and AURUM_MCP_PASSWORD in .env.

CREATE ROLE aurum_mcp_ro LOGIN PASSWORD :pw NOSUPERUSER NOCREATEDB NOCREATEROLE;

ALTER ROLE aurum_mcp_ro SET default_transaction_read_only = on;
ALTER ROLE aurum_mcp_ro SET statement_timeout = '30s';

GRANT CONNECT ON DATABASE aurum TO aurum_mcp_ro;
GRANT USAGE  ON SCHEMA gold, bronze TO aurum_mcp_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA gold, bronze TO aurum_mcp_ro;

-- Load-bearing: dbt rebuilds gold marts and bronze mirrors as DROP + CREATE, which discards
-- table-level grants. Default privileges attach SELECT to tables that do not exist yet.
ALTER DEFAULT PRIVILEGES FOR ROLE :owner IN SCHEMA gold, bronze GRANT SELECT ON TABLES TO aurum_mcp_ro;

REVOKE ALL ON SCHEMA public, silver FROM aurum_mcp_ro;
