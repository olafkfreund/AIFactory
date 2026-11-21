"""Path containment and project authorization on path/task routes (#2181)."""

from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from server.auth import _acw_principal
from server.database.engine import get_db
from server.database.models import (
    Base,
    Organization,
    OrgMember,
    Team,
    TeamMembership,
    User,
)
from server.mcp_remote.auth import AuthenticatedKey
from server.routes import (
    execution,
    files,
    stale,
    tasks,
    worktree_tools,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_DUMMY_HASH = "test-" + "hash"


def _acw(user_id: str | None, org_id: str, scopes: set[str]) -> dict[str, object]:
    key = AuthenticatedKey(
        key_id=f"k-{user_id or 'svc'}-{org_id}",
        user_id=user_id,
        org_id=org_id,
        scopes=frozenset(scopes),
    )
    return dict(_acw_principal(key))


PRINCIPALS: dict[str, dict[str, object]] = {
    "alice-member": {"id": "alice", "role": "user"},
    "alice-viewer-key": _acw("alice", "org-1", {"mcp:read"}),
    "bob-other-org": {"id": "bob", "role": "user"},
    "org1-service-read": _acw(None, "org-1", {"mcp:read"}),
    "org2-service-write": _acw(None, "org-2", {"project:write", "task:write"}),
}


@pytest.fixture
def authz_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    p1 = tmp_path / "workspace" / "p1"
    p2 = tmp_path / "workspace" / "p2"
    for proj in (p1, p2):
        (proj / "src").mkdir(parents=True)
        (proj / "src" / "main.py").write_text("print('ok')\n")
        (proj / "index.html").write_text("<html><body>hello</body></html>")
        (proj / ".git").mkdir()
        spec_dir = proj / ".aifactory" / "specs" / "001-demo"
        spec_dir.mkdir(parents=True)
        plan_file = spec_dir / "implementation_plan.json"
        plan_file.write_text('{"status": "in_progress", "phase": "coding"}')
        req_file = spec_dir / "requirements.json"
        req_file.write_text(
            '{"title": "demo", "description": "desc", "created_at": "2020-01-01T00:00:00+00:00"}'
        )
        for f in (plan_file, req_file, spec_dir):
            os.utime(f, (1577836800, 1577836800))

    projects_data = {
        "p1": {
            "path": str(p1.resolve()),
            "name": "p1",
            "slug": "p1-slug",
            "org_id": "org-1",
            "team_id": "team-1",
        },
        "p2": {
            "path": str(p2.resolve()),
            "name": "p2",
            "slug": "p2-slug",
            "org_id": "org-2",
            "team_id": "team-2",
        },
    }

    async def _aload():
        return projects_data

    monkeypatch.setattr("server.project_registry.load_projects", lambda: projects_data)
    monkeypatch.setattr("server.project_registry.aload_projects", _aload)
    monkeypatch.setattr("server.routes.tasks.load_projects", lambda: projects_data)
    monkeypatch.setattr(
        "server.routes.task_service.load_projects", lambda: projects_data
    )
    monkeypatch.setattr("server.routes.execution.load_projects", lambda: projects_data)
    monkeypatch.setattr("server.routes.stale.load_projects", lambda: projects_data)
    monkeypatch.setattr(
        "server.routes.files.browse_roots",
        lambda: [(tmp_path / "workspace").resolve(), p1.resolve(), p2.resolve()],
    )
    monkeypatch.setattr("server.routes.project_authz._auth_disabled", lambda: False)
    monkeypatch.setattr(files.get_settings(), "DISABLE_AUTH", False)

    engine = create_async_engine(
        f"sqlite+aiosqlite:///file:pc-{secrets.token_hex(8)}?mode=memory&cache=shared&uri=true"
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions() as s:
            s.add_all(
                [
                    User(
                        id="alice",
                        email="a@x",
                        name="A",
                        password_hash=_DUMMY_HASH,
                        role="user",
                    ),
                    User(
                        id="bob",
                        email="b@x",
                        name="B",
                        password_hash=_DUMMY_HASH,
                        role="user",
                    ),
                    Organization(id="org-1", name="O1", slug="o1", owner_id="alice"),
                    Organization(id="org-2", name="O2", slug="o2", owner_id="bob"),
                    OrgMember(org_id="org-1", user_id="alice", role="member"),
                    OrgMember(org_id="org-2", user_id="bob", role="owner"),
                    Team(
                        id="team-1",
                        org_id="org-1",
                        name="Default",
                        slug="default",
                        is_default=True,
                    ),
                    Team(
                        id="team-2",
                        org_id="org-2",
                        name="Default",
                        slug="default",
                        is_default=True,
                    ),
                    TeamMembership(team_id="team-1", user_id="alice", role="member"),
                    TeamMembership(team_id="team-2", user_id="bob", role="owner"),
                ]
            )
            await s.commit()

    asyncio.run(_seed())

    app = FastAPI()

    @app.middleware("http")
    async def _principal(request: Request, call_next):
        cred = request.headers.get("X-Principal", "")
        request.state.user = PRINCIPALS.get(cred)
        return await call_next(request)

    async def _db():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.include_router(files.router, prefix="/api/files")
    app.include_router(execution.router, prefix="/api/tasks")
    app.include_router(tasks.router, prefix="/api/tasks")
    app.include_router(stale.router)

    with TestClient(app) as client:
        yield client, p1, p2, tmp_path / "workspace"

    asyncio.run(engine.dispose())


def test_files_read_enforces_project_viewer_and_404s_foreign_project(authz_env):
    client, p1, p2, _ = authz_env
    # Alice can read p1
    r = client.get(
        "/api/files/read",
        params={"path": str(p1 / "src" / "main.py")},
        headers={"X-Principal": "alice-member"},
    )
    assert r.status_code == 200
    assert r.json()["content"] == "print('ok')\n"

    # Alice cannot read p2 -> uniform 404
    r = client.get(
        "/api/files/read",
        params={"path": str(p2 / "src" / "main.py")},
        headers={"X-Principal": "alice-member"},
    )
    assert r.status_code == 404

    # Host file outside registered projects -> 403 containment
    r = client.get(
        "/api/files/read",
        params={"path": "/etc/passwd"},
        headers={"X-Principal": "alice-member"},
    )
    assert r.status_code == 403


def test_files_list_and_discover_filter_invisible_projects_and_404_direct(authz_env):
    client, _p1, p2, workspace = authz_env
    # Listing parent workspace filters out p2 for Alice
    r = client.get(
        "/api/files/list",
        params={"path": str(workspace)},
        headers={"X-Principal": "alice-member"},
    )
    assert r.status_code == 200
    names = [e["name"] for e in r.json()["entries"]]
    assert names == ["p1"]

    # Discovering projects in workspace filters out p2 for Alice
    r = client.get(
        "/api/files/discover",
        params={"base_path": str(workspace)},
        headers={"X-Principal": "alice-member"},
    )
    assert r.status_code == 200
    assert [p["name"] for p in r.json()] == ["p1"]

    # Direct listing inside p2 returns 404 for Alice
    r = client.get(
        "/api/files/list",
        params={"path": str(p2 / "src")},
        headers={"X-Principal": "alice-member"},
    )
    assert r.status_code == 404


def test_files_serve_authenticates_acw_token_and_enforces_project_role(
    authz_env, monkeypatch: pytest.MonkeyPatch
):
    client, p1, p2, _ = authz_env
    acw_value = "acw_" + "alice"

    async def _fake_auth(header: str) -> AuthenticatedKey:
        presented = header.removeprefix("Bearer ")
        if presented == acw_value:
            return AuthenticatedKey(
                key_id="k-alice",
                user_id="alice",
                org_id="org-1",
                scopes=frozenset({"mcp:read"}),
            )
        raise RuntimeError("bad key")

    monkeypatch.setattr("server.mcp_remote.auth.authenticate_rest", _fake_auth)

    # Valid acw_ query token on visible project p1 -> 200
    r = client.get(
        "/api/files/serve",
        params={"path": str(p1 / "index.html"), "root": str(p1), "token": acw_value},
    )
    assert r.status_code == 200

    # Valid acw_ query token on foreign project p2 -> 404
    r = client.get(
        "/api/files/serve",
        params={"path": str(p2 / "index.html"), "root": str(p2), "token": acw_value},
    )
    assert r.status_code == 404


def test_tasks_create_and_list_enforce_project_role(authz_env):
    client, _, _, _ = authz_env
    # GET /api/tasks?project_id=p2 for Alice -> uniform 404 (not empty 200)
    r = client.get(
        "/api/tasks",
        params={"project_id": "p2"},
        headers={"X-Principal": "alice-member"},
    )
    assert r.status_code == 404

    # GET /api/tasks without project_id filters to p1 only
    r = client.get("/api/tasks", headers={"X-Principal": "alice-member"})
    assert r.status_code == 200
    assert [t["project_id"] for t in r.json()["tasks"]] == ["p1"]

    # POST /api/tasks with viewer scope ceiling -> 403
    r = client.post(
        "/api/tasks",
        json={"project_id": "p1", "title": "New", "description": "Desc"},
        headers={"X-Principal": "alice-viewer-key"},
    )
    assert r.status_code == 403

    # POST /api/tasks on foreign project -> 404
    r = client.post(
        "/api/tasks",
        json={"project_id": "p2", "title": "New", "description": "Desc"},
        headers={"X-Principal": "alice-member"},
    )
    assert r.status_code == 404

    # POST /api/tasks with member role -> 201
    r = client.post(
        "/api/tasks",
        json={"project_id": "p1", "title": "New", "description": "Desc"},
        headers={"X-Principal": "alice-member"},
    )
    assert r.status_code == 201


def test_running_tasks_and_stale_tasks_filter_by_accessible_projects(
    authz_env, monkeypatch: pytest.MonkeyPatch
):
    client, _, _, _ = authz_env
    fake_svc = SimpleNamespace(
        get_running_task_ids=lambda: ["p1:001-demo", "p2:001-demo"]
    )
    monkeypatch.setattr(execution, "get_agent_service", lambda: fake_svc)

    r = client.get("/api/tasks/running", headers={"X-Principal": "alice-member"})
    assert r.status_code == 200
    assert r.json()["tasks"] == ["p1:001-demo"]

    r = client.get(
        "/api/maintenance/stale-tasks", headers={"X-Principal": "alice-member"}
    )
    assert r.status_code == 200
    assert [t.get("task_id") or t.get("id") for t in r.json()["tasks"]] == [
        "p1:001-demo"
    ]

    # Reap with dry_run=false requires member; viewer key sees 0 eligible projects
    r = client.post(
        "/api/maintenance/stale-tasks/reap",
        params={"dry_run": "false"},
        headers={"X-Principal": "alice-viewer-key"},
    )
    assert r.status_code == 200
    assert r.json()["tasks"] == []


def test_from_plan_resolves_slug_before_project_role_check(
    authz_env, monkeypatch: pytest.MonkeyPatch
):
    client, _, _, _ = authz_env
    monkeypatch.setattr(
        execution,
        "resolve_project_id",
        lambda raw: {"p1-slug": "p1", "p2-slug": "p2"}.get(raw),
    )

    # Foreign slug -> 404
    r = client.post(
        "/api/tasks/from-plan",
        params={"project_id": "p2-slug", "title": "T", "description": "D"},
        json={"plan": {"feature": "x"}},
        headers={"X-Principal": "alice-member"},
    )
    assert r.status_code == 404

    # Visible slug with viewer scope ceiling -> 403 (before plan verification 422)
    r = client.post(
        "/api/tasks/from-plan",
        params={"project_id": "p1-slug", "title": "T", "description": "D"},
        json={"plan": {"feature": "x"}},
        headers={"X-Principal": "alice-viewer-key"},
    )
    assert r.status_code == 403


def test_worktree_open_in_ide_and_terminal_enforce_member_role(
    authz_env, monkeypatch: pytest.MonkeyPatch
):
    client, p1, p2, _ = authz_env
    monkeypatch.setattr(worktree_tools.subprocess, "Popen", MagicMock())

    # Foreign project path -> 404
    for endpoint, payload in (
        ("/api/tasks/worktree/open-in-ide", {"worktreePath": str(p2), "ide": "vscode"}),
        (
            "/api/tasks/worktree/open-in-terminal",
            {"worktreePath": str(p2), "terminal": "kitty"},
        ),
    ):
        r = client.post(endpoint, json=payload, headers={"X-Principal": "alice-member"})
        assert r.status_code == 404

        # Viewer key on visible project -> 403
        own_payload = {**payload, "worktreePath": str(p1)}
        r = client.post(
            endpoint, json=own_payload, headers={"X-Principal": "alice-viewer-key"}
        )
        assert r.status_code == 403

        # Member on visible project -> 200
        r = client.post(
            endpoint, json=own_payload, headers={"X-Principal": "alice-member"}
        )
        assert r.status_code == 200
