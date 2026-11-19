"""Registry API on the projects routes: slug, rename, by-slug, archive (#2157)."""
# mypy: ignore-errors

# ruff: noqa: S106, PLC0415 - fake /tmp paths, inline SQL and lazy imports in test setup

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server import project_registry as reg
from server.database.models import Base, Organization, OrgMember, User
from server.routes import projects as projects_mod
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
def api(tmp_path, monkeypatch):
    """The projects router with a caller picked by ``X-Test-User`` (``svc`` = service principal)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions() as s:
            for uid in ("alice", "bob"):
                s.add(User(id=uid, email=f"{uid}@x.test", password_hash="x"))
            await s.flush()
            s.add(Organization(id="org-a", name="a", slug="a", owner_id="alice"))
            s.add(Organization(id="org-b", name="b", slug="b", owner_id="bob"))
            await s.flush()
            s.add(OrgMember(org_id="org-a", user_id="alice", role="admin"))
            s.add(OrgMember(org_id="org-b", user_id="bob", role="admin"))
            await s.commit()

    asyncio.run(_seed())
    monkeypatch.setattr(projects_mod, "browse_roots", lambda: [tmp_path])
    monkeypatch.setattr("server.routes.project_authz._auth_disabled", lambda: False)

    app = FastAPI()

    @app.middleware("http")
    async def _who(request, call_next):
        who = request.headers.get("X-Test-User", "svc")
        request.state.user = (
            {"id": "default", "is_service": True} if who == "svc" else {"id": who}
        )
        return await call_next(request)

    async def _db():
        async with sessions() as s:
            yield s

    app.dependency_overrides[projects_mod.get_db] = _db
    app.include_router(projects_mod.router, prefix="/api/projects")
    return TestClient(app), tmp_path


def _make(client, tmp_path, name, **extra):
    (tmp_path / name).mkdir(exist_ok=True)
    return client.post(
        "/api/projects", json={"path": str(tmp_path / name), "name": name, **extra}
    )


def test_create_returns_slug_and_writes_no_projects_json(api):
    client, tmp = api
    r = _make(client, tmp, "My App")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"] == "my-app" and body["teamId"] and body["repoIdentity"] is None
    assert not (tmp / "registry-data" / "projects.json").exists()
    assert body["id"] in reg.load_projects()
    assert [p["slug"] for p in client.get("/api/projects").json()] == ["my-app"]


def test_create_with_explicit_slug_conflict_and_invalid(api):
    client, tmp = api
    assert _make(client, tmp, "a1", slug="shared").status_code == 201
    assert _make(client, tmp, "a2", slug="shared").status_code == 409
    assert _make(client, tmp, "a3", slug="Not Valid").status_code == 400


def test_slug_only_put_renames_and_keeps_the_id(api):
    client, tmp = api
    pid = _make(client, tmp, "first").json()["id"]
    other = _make(client, tmp, "second").json()["id"]
    r = client.put(f"/api/projects/{pid}", json={"slug": "renamed"})
    assert (
        r.status_code == 200 and r.json()["slug"] == "renamed" and r.json()["id"] == pid
    )
    assert (
        client.put(f"/api/projects/{other}", json={"slug": "renamed"}).status_code
        == 409
    )
    assert (
        client.put(f"/api/projects/{pid}", json={"slug": "Bad_Slug"}).status_code == 400
    )
    assert client.put(f"/api/projects/{pid}", json={}).status_code == 422


def test_by_slug_found_missing_and_ambiguous(api):
    client, tmp = api
    pid = _make(client, tmp, "findme").json()["id"]
    assert client.get("/api/projects/by-slug/findme").json()["id"] == pid
    assert client.get("/api/projects/by-slug/nope").status_code == 404
    # same slug in a second org -> ambiguous unless org_id given
    reg.save_projects(
        {
            "x": {
                "path": str(tmp / "x"),
                "name": "findme",
                "org_id": "org-b",
                "slug": "findme",
            }
        }
    )
    assert client.get("/api/projects/by-slug/findme").status_code == 409
    assert client.get("/api/projects/by-slug/findme?org_id=org-b").json()["id"] == "x"


def test_by_slug_respects_org_membership(api):
    client, tmp = api
    reg.save_projects(
        {
            "pa": {
                "path": str(tmp / "pa"),
                "name": "pa",
                "org_id": "org-a",
                "slug": "team-a",
            }
        }
    )
    ok = client.get("/api/projects/by-slug/team-a", headers={"X-Test-User": "alice"})
    assert ok.status_code == 200
    # bob is in another org: the slug is invisible to him, not a 403 leak
    assert (
        client.get(
            "/api/projects/by-slug/team-a", headers={"X-Test-User": "bob"}
        ).status_code
        == 404
    )


def test_a_slug_equal_to_a_subroute_segment_still_resolves(api):
    client, tmp = api
    pid = _make(client, tmp, "github").json()["id"]
    assert client.get("/api/projects/by-slug/github").json()["id"] == pid


def test_delete_archives_and_blocks_while_running(api, monkeypatch):
    client, tmp = api
    pid = _make(client, tmp, "doomed").json()["id"]
    running = [f"{pid}:001-x"]
    fake = SimpleNamespace(get_running_task_ids=lambda: running)
    monkeypatch.setattr("server.services.agent_service.get_agent_service", lambda: fake)
    assert client.delete(f"/api/projects/{pid}").status_code == 409
    assert pid in reg.load_projects()
    running.clear()
    assert client.delete(f"/api/projects/{pid}").status_code == 204
    assert pid not in reg.load_projects()
    assert client.get("/api/projects").json() == []
    assert client.get(f"/api/projects/{pid}").status_code == 404
    from server import project_store

    assert project_store.get_project(pid, include_archived=True)["slug"] == "doomed"
    # the slug stays reserved
    assert _make(client, tmp, "doomed").json()["slug"] == "doomed-2"


def test_by_slug_never_returns_stored_credentials(api):
    client, tmp = api
    pid = _make(client, tmp, "secretive").json()["id"]
    data = reg.load_projects()
    data[pid]["settings"] = {"githubToken": "ghp_x", "gitToken": "t", "model": "m"}
    reg.save_projects(data)
    body = client.get("/api/projects/by-slug/secretive").json()
    assert "githubToken" not in body["settings"] and "gitToken" not in body["settings"]
    assert body["settings"]["model"] == "m"
    assert "ghp_x" not in client.get("/api/projects/by-slug/secretive").text
