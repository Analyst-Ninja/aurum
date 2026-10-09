"""AURUM MCP server: a read-only SQL window onto the gold marts (stdio transport).

    uv run --group mcp python -m src.mcp.server
"""

import functools
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import catalog, db

mcp = MCPServer("aurum")


def tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Register ``fn`` as a tool, with expected failures reaching the client as text.

    The SDK shows only "Error executing tool X" for a plain exception (it treats it as a
    crash). Our ValueError / RuntimeError messages are deliberate - "contains DELETE",
    "SQLSTATE 42501" - and the client needs them to fix its query, so they become ToolError.
    """

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


@mcp.resource("aurum://catalog")
def catalog_resource() -> str:
    """Every table and column with descriptions, as markdown."""
    return catalog.to_markdown()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
