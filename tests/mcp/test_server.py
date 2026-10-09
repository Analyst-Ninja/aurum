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


def test_transport_defaults_to_stdio(monkeypatch):
    for name in ("AURUM_MCP_TRANSPORT", "AURUM_MCP_LISTEN_HOST", "AURUM_MCP_LISTEN_PORT"):
        monkeypatch.delenv(name, raising=False)

    assert server.listen_config() == ("stdio", "127.0.0.1", 8000)


def test_http_transport_reads_host_and_port(monkeypatch):
    monkeypatch.setenv("AURUM_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("AURUM_MCP_LISTEN_HOST", "localhost")
    monkeypatch.setenv("AURUM_MCP_LISTEN_PORT", "9001")

    assert server.listen_config() == ("streamable-http", "localhost", 9001)


def test_http_transport_refuses_a_public_bind_without_the_override(monkeypatch):
    import pytest

    monkeypatch.setenv("AURUM_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("AURUM_MCP_LISTEN_HOST", "0.0.0.0")
    monkeypatch.delenv("AURUM_MCP_ALLOW_PUBLIC_BIND", raising=False)

    with pytest.raises(ValueError, match="Refusing to serve unauthenticated MCP"):
        server.listen_config()

    monkeypatch.setenv("AURUM_MCP_ALLOW_PUBLIC_BIND", "1")
    assert server.listen_config()[1] == "0.0.0.0"


def test_unknown_transport_is_rejected(monkeypatch):
    import pytest

    monkeypatch.setenv("AURUM_MCP_TRANSPORT", "sse")

    with pytest.raises(ValueError, match="stdio or streamable-http"):
        server.listen_config()
