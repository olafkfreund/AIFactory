#!/usr/bin/env python3
"""Tests for the new mcp__aifactory__project_create tool.

The tool wraps POST /api/projects (the same endpoint #82 PR-A extended),
exposing both local-mode (path) and clone-mode (git_url) to MCP clients —
notably the /handover skill, which uses it to auto-register the user's
cwd when no matching project exists.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# conftest.py pre-mocks claude_agent_sdk for tests that don't need the
# real SDK. These tests DO — without the real `@tool` decorator the tools
# list ends up populated with MagicMocks instead of SdkMcpTool dataclasses.
# Drop the mock + the cached task_control module so the import re-binds
# to the real SDK. (Same trick test_mcp_task_control.py uses.)
if isinstance(sys.modules.get("claude_agent_sdk"), MagicMock):
    sys.modules.pop("claude_agent_sdk", None)
    sys.modules.pop("claude_agent_sdk.types", None)
    sys.modules.pop("agents.tools_pkg.tools.task_control", None)


def _get_project_create_tool():
    """Return the SdkMcpTool wrapper for project_create from the registry."""
    from agents.tools_pkg.tools.task_control import create_task_control_tools
    for t in create_task_control_tools():
        if t.name == "project_create":
            return t
    raise AssertionError("project_create not registered in MCP tool list")


def test_project_create_is_in_the_registry():
    tool = _get_project_create_tool()
    assert tool.name == "project_create"
    assert "git_url" in tool.description.lower() or "clone" in tool.description.lower()


@pytest.mark.asyncio
async def test_project_create_rejects_neither_path_nor_url():
    """Mirror of the backend's ProjectCreate model_validator — surface
    the validation error at the MCP boundary rather than letting a 422
    bubble back from the server."""
    tool = _get_project_create_tool()
    result = await tool.handler({"confirm": True})
    assert result.get("isError") is True
    text = result["content"][0]["text"]
    assert "path" in text and "git_url" in text


@pytest.mark.asyncio
async def test_project_create_rejects_both_path_and_url():
    tool = _get_project_create_tool()
    result = await tool.handler(
        {"path": "/x", "git_url": "https://example.test/r", "confirm": True}
    )
    assert result.get("isError") is True
    text = result["content"][0]["text"]
    assert "mutually exclusive" in text


@pytest.mark.asyncio
async def test_project_create_confirm_gate_blocks_without_confirm():
    """No confirm=true → return a structured preview instead of touching disk."""
    tool = _get_project_create_tool()
    result = await tool.handler({"git_url": "https://example.test/me/r.git"})
    import json
    body = json.loads(result["content"][0]["text"])
    assert body["requires_confirmation"] is True
    assert body["verb"] == "create_project"
    assert body["preview"]["mode"] == "clone"
    assert body["preview"]["git_url"] == "https://example.test/me/r.git"


@pytest.mark.asyncio
async def test_project_create_clone_mode_posts_gitUrl():
    """confirm=true with git_url → POST /api/projects with camelCase gitUrl."""
    from unittest.mock import AsyncMock

    captured: dict = {}

    async def fake_request(method, path, **kw):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kw.get("json")
        return {"id": "proj-xyz", "name": "r", "path": "/tmp/r"}

    # Build a fresh tool tied to the mocked request callable. The factory
    # accepts the request function as an arg — but here we monkeypatch
    # the module-level `request` instead because the existing
    # create_task_control_tools() closure captures it.
    import agents.tools_pkg.tools.task_control as tc

    original_request = getattr(tc, "request", None)
    tc.request = AsyncMock(side_effect=fake_request)
    try:
        tool = _get_project_create_tool()
        # Note: the closure was captured at tool-build time, so re-build
        # after the patch to get a tool that sees the mocked request.
        for t in tc.create_task_control_tools():
            if t.name == "project_create":
                tool = t
                break
        result = await tool.handler(
            {
                "git_url": "https://example.test/me/r.git",
                "branch": "main",
                "name": "r",
                "confirm": True,
            }
        )
    finally:
        if original_request is not None:
            tc.request = original_request

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/projects"
    assert captured["json"] == {
        "gitUrl": "https://example.test/me/r.git",
        "branch": "main",
        "name": "r",
    }
    import json
    body = json.loads(result["content"][0]["text"])
    assert body["created"] is True
    assert body["project_id"] == "proj-xyz"


@pytest.mark.asyncio
async def test_project_create_local_mode_posts_path():
    from unittest.mock import AsyncMock

    captured: dict = {}

    async def fake_request(method, path, **kw):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kw.get("json")
        return {"id": "proj-local", "path": "/tmp/x"}

    import agents.tools_pkg.tools.task_control as tc
    original = getattr(tc, "request", None)
    tc.request = AsyncMock(side_effect=fake_request)
    try:
        for t in tc.create_task_control_tools():
            if t.name == "project_create":
                tool = t
                break
        await tool.handler({"path": "/tmp/x", "confirm": True})
    finally:
        if original is not None:
            tc.request = original

    assert captured["json"] == {"path": "/tmp/x"}
