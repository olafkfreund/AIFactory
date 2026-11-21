"""Copilot MCP task tools check the resolved task's project before file access."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

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
    monkeypatch.setattr(copilot_mcp.engine, "async_session_factory", Session)
    denied = AsyncMock(side_effect=HTTPException(404, "Project not found"))
    monkeypatch.setattr(copilot_mcp, "require_project_role", denied)
    with pytest.raises(HTTPException) as exc:
        await copilot_mcp._dispatch_tool(
            "aifactory_get_spec", {}, None, "task", {"id": "other"}
        )
    assert exc.value.status_code == 404
    assert denied.await_args.args[1:3] == ("project", "viewer")


def test_dedicated_mcp_credential_is_project_bound(monkeypatch):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setenv("AIFACTORY_MCP_SECRET", "test-secret")
    monkeypatch.setenv("AIFACTORY_MCP_PROJECT_ID", "own")
    monkeypatch.setattr(copilot_mcp.engine, "async_session_factory", Session)
    monkeypatch.setattr(
        copilot_mcp,
        "load_projects",
        lambda: {"own": {"org_id": "org-a"}, "other": {"org_id": "org-b"}},
    )
    monkeypatch.setattr(copilot_mcp, "_tool_get_spec", AsyncMock(return_value="spec"))
    authorize = AsyncMock(return_value=None)
    monkeypatch.setattr(copilot_mcp, "require_project_role", authorize)
    target = {"project_id": "other"}
    monkeypatch.setattr(
        copilot_mcp, "project_id_for_path", lambda _path: target["project_id"]
    )
    monkeypatch.setattr(
        copilot_mcp,
        "_resolve_task",
        AsyncMock(return_value=(Path("/repo/.aifactory/specs/task"), {})),
    )
    app = FastAPI()

    @app.middleware("http")
    async def legacy_identity(request, call_next):
        request.state.user = {"id": "default", "is_legacy_shared_token": True}
        return await call_next(request)

    app.include_router(copilot_mcp.router)
    with TestClient(app) as client:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "aifactory_get_spec", "arguments": {"task_id": "task"}},
        }
        headers = {"Authorization": "Bearer test-secret"}
        assert client.post("/mcp", json=body, headers=headers).status_code == 404
        assert authorize.await_count == 0
        target["project_id"] = "own"
        assert client.post("/mcp", json=body, headers=headers).status_code == 200
        assert authorize.await_args.args[0]["org_id"] == "org-a"
