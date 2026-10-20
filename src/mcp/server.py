"""AURUM MCP server: a read-only SQL window onto the gold marts, plus model decisions.

    uv run --group mcp python -m src.mcp.server                       # stdio (default)
    AURUM_MCP_TRANSPORT=streamable-http uv run --group mcp python -m src.mcp.server
"""

import functools
import inspect
import os
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import catalog, db
from src.utils.env import load_env

mcp = MCPServer("aurum")


def tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Register ``fn`` as a tool, with expected failures reaching the client as text.

    The SDK shows only "Error executing tool X" for a plain exception (it treats it as a
    crash). Our ValueError / RuntimeError messages are deliberate - "contains DELETE",
    "SQLSTATE 42501" - and the client needs them to fix its query, so they become ToolError.
    """

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except (ValueError, RuntimeError) as error:
                raise ToolError(str(error)) from None

    else:

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except (ValueError, RuntimeError) as error:
                raise ToolError(str(error)) from None

    return mcp.tool()(wrapper)


@tool
def list_tables(schema: str = "gold") -> list[dict[str, Any]]:
    """List tables in a schema with their description and column count."""
    return catalog.list_tables(schema)


@tool
def describe_table(table: str, schema: str = "gold") -> dict[str, Any]:
    """Columns (name, type, description) of one table. Looks it up live if not cached."""
    return catalog.describe(schema, table)


@tool
def run_query(sql: str, max_rows: int = 1000) -> dict[str, Any]:
    """Run one read-only SQL query. Returns columns, rows, row_count, truncated, elapsed_ms."""
    return db.run_select(sql, max_rows=max_rows)


@tool
def refresh_catalog() -> int:
    """Reload the table catalog (run after a dbt build). Returns the table count."""
    return catalog.refresh()


async def _decisions(version: str | None, asof: str | None, **view: Any) -> dict[str, Any]:
    """Score one day with the newest narrowed run. Heavy imports stay inside the call, so
    the SQL tools and server start-up do not pay for LightGBM."""
    try:
        from src.inference import run
        from src.modeling.config import load_config
    except ImportError as error:
        raise RuntimeError(
            "Decisions need the modeling dependencies: uv run --group mcp --group modeling ..."
        ) from error

    load_env()
    config = load_config(os.getenv("AURUM_INFERENCE_CONFIG", run.DEFAULT_CONFIG))
    result, meta = await run.decide_day(
        db.get_async_engine(), config, version or "latest-narrow", asof
    )
    return run.to_payload(result, meta, **view)


@tool
async def get_decisions(
    asof: str | None = None, action: str | None = None, limit: int = 50, version: str | None = None
) -> dict[str, Any]:
    """BUY / SELL / HOLD for the S&P 500 from the newest trained model (top decile BUY,
    bottom decile SELL). A relative ranking, not a price forecast - read `caveat`.
    `asof` is a past date (default: newest, must be fresh); `action` filters to one label."""
    return await _decisions(version, asof, action=action, limit=limit)


@tool
async def score_symbols(
    symbols: list[str], asof: str | None = None, version: str | None = None
) -> dict[str, Any]:
    """Decision, decile (1-10) and score for the named tickers. Deciles are ranked against
    the full day's universe, so the answer matches `get_decisions`."""
    return await _decisions(version, asof, symbols=symbols, limit=len(symbols) or 1)


@mcp.resource("aurum://catalog")
def catalog_resource() -> str:
    """Every table and column with descriptions, as markdown."""
    return catalog.to_markdown()


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def listen_config() -> tuple[str, str, int]:
    """Transport, listen host and listen port from the environment.

    stdio stays the default so ``.mcp.json`` keeps working. The HTTP transport has no
    authentication of its own, so it binds loopback only - reach it through an SSM
    port-forward - unless ``AURUM_MCP_ALLOW_PUBLIC_BIND=1`` says the caller put something
    in front of it. ``HOST``/``PORT`` are the Postgres endpoint, hence the separate names.
    """
    load_env()
    transport = os.getenv("AURUM_MCP_TRANSPORT", "stdio")
    host = os.getenv("AURUM_MCP_LISTEN_HOST", "127.0.0.1")
    port = int(os.getenv("AURUM_MCP_LISTEN_PORT", "8000"))
    if transport not in ("stdio", "streamable-http"):
        raise ValueError(f"AURUM_MCP_TRANSPORT must be stdio or streamable-http, got {transport!r}")
    if (
        transport == "streamable-http"
        and host not in LOOPBACK_HOSTS
        and os.getenv("AURUM_MCP_ALLOW_PUBLIC_BIND") != "1"
    ):
        raise ValueError(
            f"Refusing to serve unauthenticated MCP on {host!r}; use a loopback address "
            "or set AURUM_MCP_ALLOW_PUBLIC_BIND=1"
        )
    return transport, host, port


def main() -> None:
    transport, host, port = listen_config()
    if transport == "stdio":
        mcp.run()
        return
    try:
        catalog.get_catalog()  # warm the cache so the first client call is not the slow one
    except Exception:  # a database that is down at boot must not stop the server serving
        db.logger.warning("catalog warm-up failed; it will load on first use", exc_info=True)
    mcp.run("streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
