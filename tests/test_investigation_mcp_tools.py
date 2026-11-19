"""Standalone investigation tools call only caller-authorized HTTP proxies."""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

if isinstance(sys.modules.get("claude_agent_sdk"), MagicMock):
    sys.modules.pop("claude_agent_sdk", None)
    sys.modules.pop("claude_agent_sdk.types", None)
    sys.modules.pop("agents.tools_pkg.tools.task_control", None)

from agents.tools_pkg.tools import task_control
from claude_agent_sdk import create_sdk_mcp_server
from mcp.types import ListToolsRequest
from mcp_server.aifactory_server import _control_tools


@pytest.mark.asyncio
async def test_stdio_tool_listing_follows_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def names() -> set[str]:
        server = create_sdk_mcp_server(
            name="test", version="1", tools=_control_tools()
        )["instance"]
        result = await server.request_handlers[ListToolsRequest](
            ListToolsRequest(method="tools/list")
        )
        return {tool.name for tool in result.root.tools}

    monkeypatch.setenv("AIFACTORY_INVESTIGATIONS", "off")
    assert not any(name.startswith("investigation_") for name in await names())
    monkeypatch.setenv("AIFACTORY_INVESTIGATIONS", "on")
    assert {name for name in await names() if name.startswith("investigation_")} == {
        "investigation_create",
        "investigation_get",
        "investigation_logs",
        "investigation_lease",
        "investigation_stop",
    }
    monkeypatch.setenv("AIFACTORY_INVESTIGATIONS", "drain")
    assert {name for name in await names() if name.startswith("investigation_")} == {
        "investigation_get",
        "investigation_logs",
        "investigation_lease",
        "investigation_stop",
    }


@pytest.mark.asyncio
async def test_five_tools_and_get_invokes_caller_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_request(method: str, path: str, **_kwargs: object) -> dict[str, str]:
        calls.append((method, path))
        return {"state": "running"}

    monkeypatch.setattr(task_control, "request", fake_request)
    tools = {
        tool.name: tool.handler for tool in task_control.create_investigation_tools()
    }
    assert set(tools) == {
        "investigation_create",
        "investigation_get",
        "investigation_logs",
        "investigation_lease",
        "investigation_stop",
    }
    run_id = str(uuid4())
    result = await tools["investigation_get"]({"id": run_id})
    assert calls == [("GET", f"/api/investigations/{run_id}")]
    assert json.loads(result["content"][0]["text"]) == {"state": "running"}


@pytest.mark.asyncio
async def test_invalid_id_never_reaches_http(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("HTTP request should not be made")

    monkeypatch.setattr(task_control, "request", forbidden)
    tools = {
        tool.name: tool.handler for tool in task_control.create_investigation_tools()
    }
    result = await tools["investigation_stop"]({"id": "../../other"})
    assert result["isError"] is True
