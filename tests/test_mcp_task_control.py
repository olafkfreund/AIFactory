"""Tests for the 8 task-control MCP tools.

Each tool is a thin wrapper over a REST call; we stub ``http_client.request``
and verify:
- the tool calls the right HTTP method + path + params
- the tool returns the MCP ``content[]`` envelope shape
- read tools' lean projections drop heavy fields
- write tools include the audit-able verb in their response shape
- MCPHTTPError propagates as an ``isError`` content block (not a raised exception)
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# conftest.py pre-mocks claude_agent_sdk to support test files that don't
# need the real SDK. These tests DO need the real SDK so the ``@tool``
# decorator produces actual SdkMcpTool dataclasses (not MagicMocks).
# Drop the mock before importing task_control so the import binds to the
# real ``tool`` decorator.
if isinstance(sys.modules.get("claude_agent_sdk"), MagicMock):
    sys.modules.pop("claude_agent_sdk", None)
    sys.modules.pop("claude_agent_sdk.types", None)
    # And drop the module-under-test so it re-imports the real SDK.
    sys.modules.pop("agents.tools_pkg.tools.task_control", None)

import json

import pytest
from agents.tools_pkg import http_client as hc
from agents.tools_pkg.tools.task_control import create_task_control_tools


@pytest.fixture
def tools_by_name():
    """Return ``{name: handler}`` for the 8 task-control tools.

    The Claude Agent SDK's ``@tool`` decorator produces an ``SdkMcpTool``
    dataclass with ``.name``, ``.description``, ``.input_schema`` and
    ``.handler``. Tests invoke ``.handler(args)`` directly.
    """
    tools = create_task_control_tools()
    assert tools, "claude_agent_sdk not available"
    return {t.name: t.handler for t in tools}


def _make_request_stub(monkeypatch, response, captured=None):
    """Patch ``hc.request`` (the symbol the tools import) with a stub.

    ``response`` can be a callable ``(method, path, **kwargs) -> Any``
    or a static return value.
    """
    async def stub(method, path, **kwargs):
        if captured is not None:
            captured.append({"method": method, "path": path, "kwargs": kwargs})
        if callable(response):
            return response(method, path, **kwargs)
        return response

    # Both the import site and the module level need to see the stub.
    monkeypatch.setattr(
        "agents.tools_pkg.tools.task_control.request", stub
    )


def _content_text(result):
    """Extract the text block from an MCP tool result."""
    assert "content" in result
    assert isinstance(result["content"], list)
    assert result["content"][0]["type"] == "text"
    return result["content"][0]["text"]


# ── Catalog presence ────────────────────────────────────────────────


def test_all_eight_tools_registered():
    tools = create_task_control_tools()
    names = {t.name for t in tools}
    expected = {
        "task_list",
        "task_running",
        "task_get",
        "task_status",
        "task_get_logs",
        "task_start",
        "task_stop",
        "task_approve_plan",
    }
    assert expected.issubset(names), f"missing: {expected - names}"


# ── Read tools ──────────────────────────────────────────────────────


async def test_task_list_calls_correct_endpoint(tools_by_name, monkeypatch):
    captured: list = []
    _make_request_stub(
        monkeypatch,
        [{"id": "t1", "title": "task one", "status": "running", "project_id": "p1"}],
        captured,
    )
    result = await tools_by_name["task_list"]({"status": "running", "limit": 10})

    assert captured[0]["method"] == "GET"
    assert captured[0]["path"] == "/api/tasks"
    assert captured[0]["kwargs"]["params"] == {"status": "running", "limit": 10}

    payload = json.loads(_content_text(result))
    assert payload["count"] == 1
    assert payload["tasks"][0]["id"] == "t1"


async def test_task_list_handles_wrapped_response(tools_by_name, monkeypatch):
    """Server may return ``{tasks: [...]}`` instead of a bare list."""
    _make_request_stub(monkeypatch, {"tasks": [{"id": "t9", "title": "x"}]})
    result = await tools_by_name["task_list"]({})
    payload = json.loads(_content_text(result))
    assert payload["count"] == 1
    assert payload["tasks"][0]["id"] == "t9"


async def test_task_running(tools_by_name, monkeypatch):
    captured: list = []
    _make_request_stub(
        monkeypatch,
        [{"id": "t2", "title": "running task", "phase": "coding"}],
        captured,
    )
    result = await tools_by_name["task_running"]({})
    assert captured[0]["path"] == "/api/tasks/running"
    payload = json.loads(_content_text(result))
    assert payload["count"] == 1
    assert payload["running"][0]["phase"] == "coding"


async def test_task_get_truncates_heavy_fields(tools_by_name, monkeypatch):
    huge_plan = "x" * 5000
    _make_request_stub(
        monkeypatch,
        {
            "id": "t1",
            "implementation_plan_json": huge_plan,
            "requirements_json": huge_plan,
            "status": "running",
        },
    )
    result = await tools_by_name["task_get"]({"task_id": "t1"})
    payload = json.loads(_content_text(result))
    assert "[truncated]" in payload["implementation_plan_json"]
    assert "[truncated]" in payload["requirements_json"]
    # Non-heavy field passes through
    assert payload["id"] == "t1"
    assert payload["status"] == "running"


async def test_task_status_endpoint(tools_by_name, monkeypatch):
    captured: list = []
    _make_request_stub(
        monkeypatch,
        {"phase": "planning", "overall_progress": 25, "model_in_use": "sonnet"},
        captured,
    )
    result = await tools_by_name["task_status"]({"task_id": "t3"})
    assert captured[0]["path"] == "/api/tasks/t3/status"
    payload = json.loads(_content_text(result))
    assert payload["phase"] == "planning"


async def test_task_get_logs_caps_at_500(tools_by_name, monkeypatch):
    captured: list = []
    _make_request_stub(monkeypatch, {"logs": []}, captured)
    await tools_by_name["task_get_logs"]({"task_id": "t4", "tail": 10000})
    assert captured[0]["kwargs"]["params"] == {"tail": 500}


async def test_task_get_logs_default(tools_by_name, monkeypatch):
    captured: list = []
    _make_request_stub(monkeypatch, {"logs": []}, captured)
    await tools_by_name["task_get_logs"]({"task_id": "t4"})
    assert captured[0]["kwargs"]["params"] == {"tail": 100}


# ── Write tools ─────────────────────────────────────────────────────


async def test_task_start(tools_by_name, monkeypatch):
    captured: list = []
    _make_request_stub(monkeypatch, {"ok": True}, captured)
    result = await tools_by_name["task_start"]({"task_id": "t5"})
    assert captured[0]["method"] == "POST"
    assert captured[0]["path"] == "/api/tasks/t5/start"
    payload = json.loads(_content_text(result))
    assert payload["started"] is True
    assert payload["task_id"] == "t5"


async def test_task_stop(tools_by_name, monkeypatch):
    captured: list = []
    _make_request_stub(monkeypatch, {"ok": True}, captured)
    result = await tools_by_name["task_stop"]({"task_id": "t6"})
    assert captured[0]["method"] == "POST"
    assert captured[0]["path"] == "/api/tasks/t6/stop"
    payload = json.loads(_content_text(result))
    assert payload["stopped"] is True


async def test_task_approve_plan(tools_by_name, monkeypatch):
    captured: list = []
    _make_request_stub(monkeypatch, {"ok": True}, captured)
    result = await tools_by_name["task_approve_plan"]({"task_id": "t7"})
    assert captured[0]["path"] == "/api/tasks/t7/approve-plan"
    payload = json.loads(_content_text(result))
    assert payload["approved"] is True


# ── Error propagation ──────────────────────────────────────────────


async def test_http_error_becomes_isError_content(tools_by_name, monkeypatch):
    """MCPHTTPError must NOT raise — it should land as a content block."""

    async def raise_it(method, path, **kwargs):
        raise hc.MCPHTTPError("web-server not reachable at http://x — start it")

    monkeypatch.setattr(
        "agents.tools_pkg.tools.task_control.request", raise_it
    )
    result = await tools_by_name["task_list"]({})
    assert result.get("isError") is True
    assert "not reachable" in _content_text(result)


async def test_write_error_does_not_silently_swallow(tools_by_name, monkeypatch):
    """Errors on writes must surface as visible failures."""

    async def raise_it(method, path, **kwargs):
        raise hc.MCPHTTPError("token rejected at ~/.aifactory/.token")

    monkeypatch.setattr(
        "agents.tools_pkg.tools.task_control.request", raise_it
    )
    result = await tools_by_name["task_start"]({"task_id": "t8"})
    assert result.get("isError") is True
    assert "token rejected" in _content_text(result)
