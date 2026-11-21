"""The stdio MCP projects route returns only projects visible to its caller."""

import importlib
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))
sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from server.database.engine import get_db  # noqa: E402
from server.mcp_remote.auth import AuthenticatedKey  # noqa: E402

stdio_router = importlib.import_module("server.mcp_stdio.router")
stdio_auth = importlib.import_module("server.mcp_stdio.auth")
projects_mod = importlib.import_module("server.routes.projects")


def test_proxy_list_projects_filters_other_team(monkeypatch):
    projects = {
        "p1": {"name": "alpha", "path": "/a", "org_id": "org-1"},
        "p2": {"name": "beta", "path": "/b", "org_id": "org-1"},
    }
    monkeypatch.setattr(stdio_router, "load_projects", lambda: projects)
    monkeypatch.setattr(
        projects_mod,
        "project_to_response",
        lambda pid, pdata: {"id": pid, "name": pdata["name"]},
    )

    async def authenticate(_header):
        return AuthenticatedKey("key", frozenset({"mcp:read"}), "caller", "org-1")

    async def visible(_request, _db):
        return {"p1"}

    async def db():
        yield object()

    monkeypatch.setattr(stdio_auth.mcp_remote_auth, "authenticate", authenticate)
    monkeypatch.setattr(stdio_router, "accessible_project_ids", visible)
    app = FastAPI()
    app.include_router(stdio_router.router)
    app.dependency_overrides[get_db] = db
    response = TestClient(app).get(
        "/api/mcp-stdio/projects", headers={"Authorization": "Bearer acw_caller"}
    )
    assert response.status_code == 200
    assert [project["id"] for project in response.json()] == ["p1"]
