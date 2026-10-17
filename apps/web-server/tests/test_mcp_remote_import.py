"""mcp-remote must import and mount on the locked mcp 2.x with its flag on (#1820)."""

import pytest
from mcp.types import CallToolRequestParams
from server.mcp_remote import server as srv
from server.mcp_remote.tools import get_tool_definitions


def test_flag_on_mounts_mcp_remote(monkeypatch):
    monkeypatch.setenv("AIFACTORY_MCP_REMOTE_ENABLED", "true")
    main = pytest.importorskip("server.main")

    paths = main.create_app().openapi()["paths"]

    assert "/api/mcp-remote/sse" in paths
    assert "/api/mcp-remote/messages/" in paths


async def test_handlers_list_catalog_and_refuse_without_key():
    listed = await srv._list_tools(None, None)
    assert [t.name for t in listed.tools] == [d["name"] for d in get_tool_definitions()]

    result = await srv._call_tool(
        None, CallToolRequestParams(name="aifactory.list_tasks", arguments={})
    )
    assert result.is_error is True
