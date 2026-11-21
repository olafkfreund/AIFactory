"""Copilot MCP task tools check the resolved task's project before file access."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "web-server"))

from server.routes import copilot_mcp  # noqa: E402


@pytest.mark.asyncio
async def test_resolved_task_requires_project_role(monkeypatch):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr(
        copilot_mcp,
        "_resolve_task",
        AsyncMock(return_value=(Path("/project/.aifactory/specs/task"), {})),
    )
    monkeypatch.setattr(copilot_mcp, "project_id_for_path", lambda path: "project")
    monkeypatch.setattr(copilot_mcp, "async_session_factory", Session)
    denied = AsyncMock(side_effect=HTTPException(404, "Project not found"))
    monkeypatch.setattr(copilot_mcp, "require_project_role", denied)
    with pytest.raises(HTTPException) as exc:
        await copilot_mcp._dispatch_tool(
            "aifactory_get_spec", {}, None, "task", {"id": "other"}
        )
    assert exc.value.status_code == 404
    assert denied.await_args.args[1:3] == ("project", "viewer")
