"""The four tools and the catalog resource are registered under the names clients call."""

import asyncio

from src.mcp import server


def test_tools_and_resource_are_registered():
    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    resources = {str(r.uri) for r in asyncio.run(server.mcp.list_resources())}

    assert tools == {"list_tables", "describe_table", "run_query", "refresh_catalog"}
    assert resources == {"aurum://catalog"}


def test_a_rejected_query_reaches_the_client_as_a_tool_error_with_the_reason():
    import pytest
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="contains DELETE"):
        asyncio.run(
            server.mcp.call_tool(
                "run_query", {"sql": "WITH g AS (DELETE FROM t) SELECT 1"}
            )
        )
