"""Project environment reads respect the effective project role (#2198)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.database.engine import get_db
from server.database.models import (
    Base,
    Organization,
    OrgMember,
    Team,
    TeamMembership,
    User,
)
from server.routes import context, project_authz
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_DUMMY_HASH = "x"


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def seed() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            for role in ("viewer", "member", "admin"):
                db.add(
                    User(
                        id=role, email=f"{role}@test.invalid", password_hash=_DUMMY_HASH
                    )
                )
            await db.flush()
            db.add(Organization(id="org", name="Org", slug="org", owner_id="admin"))
            await db.flush()
            db.add(
                Team(
                    id="team",
                    org_id="org",
                    name="Team",
                    slug="default",
                    is_default=True,
                )
            )
            for role in ("viewer", "member", "admin"):
                db.add(OrgMember(org_id="org", user_id=role, role=role))
                db.add(TeamMembership(team_id="team", user_id=role, role=role))
            await db.commit()

    asyncio.run(seed())
    projects = {"p": {"path": str(tmp_path), "org_id": "org", "team_id": "team"}}

    async def aload():
        return projects

    monkeypatch.setattr("server.project_registry.aload_projects", aload)
    monkeypatch.setattr("server.project_registry.load_projects", lambda: projects)
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    settings = {
        "GITHUB_TOKEN": "github-secret-sentinel",
        "GIT_TOKEN": "git-secret-sentinel",
        "GIT_BASE_URL": "https://url-secret-sentinel@example.test/repo",
        "OPENAI_API_KEY": "openai-secret-sentinel",
        "VOYAGE_API_KEY": "voyage-secret-sentinel",
        "GOOGLE_API_KEY": "google-secret-sentinel",
        "AZURE_OPENAI_API_KEY": "azure-secret-sentinel",
        "CUSTOM_MCP_SERVERS": json.dumps(
            [
                {
                    "name": "custom",
                    "type": "http",
                    "url": "https://mcp-secret-sentinel@mcp.test/path",
                    "headers": {"Authorization": "header-secret-sentinel"},
                    "env": {"KEY": "env-secret-sentinel"},
                    "args": ["args-secret-sentinel"],
                },
                {
                    "name": "command",
                    "type": "stdio",
                    "command": "npx",
                    "env": {"KEY": "command-env-secret-sentinel"},
                    "args": ["command-args-secret-sentinel"],
                },
            ]
        ),
    }
    monkeypatch.setattr(
        context.project_settings_store, "get_settings", AsyncMock(return_value=settings)
    )
    monkeypatch.setattr(
        context.subprocess,
        "run",
        lambda *a, **kw: type("Result", (), {"returncode": 1})(),
    )

    app = FastAPI()

    @app.middleware("http")
    async def principal(request, call_next):
        who = request.headers.get("X-Test-User", "viewer")
        request.state.user = {"id": who.split(":")[0]}
        if ":" in who:
            request.state.user["scopes"] = ["project:read"]
        return await call_next(request)

    async def get_test_db():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = get_test_db
    app.include_router(context.project_router, prefix="/api/projects/{projectId}")
    yield TestClient(app), projects
    asyncio.run(engine.dispose())


@pytest.mark.parametrize("who", ["viewer", "member", "admin:scoped"])
def test_non_admin_gets_explicit_allowlist(api, who):
    client, _ = api
    response = client.get("/api/projects/p/env", headers={"X-Test-User": who})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["gitBaseUrl"] == "https://example.test/repo"
    assert data["githubTokenSet"] is True
    assert all(data["graphitiKeysSet"].values())
    assert data["customMcpServers"] == [
        {"name": "custom", "type": "http", "host": "mcp.test"},
        {"name": "command", "type": "stdio", "command": "npx"},
    ]
    assert "secret-sentinel" not in json.dumps(data)
    assert "gitToken" not in data and "graphitiProviderConfig" not in data


def test_admin_gets_full_payload_and_member_cannot_patch(api):
    client, _ = api
    data = client.get("/api/projects/p/env", headers={"X-Test-User": "admin"}).json()[
        "data"
    ]
    assert data["gitToken"] == "git-secret-sentinel"
    assert data["graphitiProviderConfig"]["openaiApiKey"] == "openai-secret-sentinel"
    response = client.patch(
        "/api/projects/p/env", json={}, headers={"X-Test-User": "member"}
    )
    assert response.status_code == 403


def test_unknown_and_invisible_project_have_same_404(api):
    client, projects = api
    missing = client.get("/api/projects/missing/env")
    projects["p"]["team_id"] = "other-team"
    invisible = client.get("/api/projects/p/env")
    assert missing.status_code == invisible.status_code == 404
    assert missing.json() == invisible.json()
