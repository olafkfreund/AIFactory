"""Autonomy ceiling and agent identity on ``acw_`` keys (Factory#1960 slice 1)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import Request
from pydantic import ValidationError

_WEB_SERVER_DIR = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER_DIR))

from server.database.models import ApiKey  # noqa: E402
from server.routes.api_keys import CreateApiKeyRequest  # noqa: E402

_VERSIONS = _WEB_SERVER_DIR / "server" / "database" / "alembic" / "versions"


def test_key_with_ceiling_carries_it_and_its_agent_identity():
    key = ApiKey(id="k-123", autonomy_ceiling="medium")
    assert key.autonomy_ceiling == "medium"
    assert key.agent_identity == "agent:aifactory/k-123"


def test_key_without_ceiling_records_null_not_a_default():
    key = ApiKey(id="k-456")
    assert key.autonomy_ceiling is None
    assert key.agent_identity == "agent:aifactory/k-456"


def test_create_request_ceiling_is_optional_and_validated():
    assert CreateApiKeyRequest(name="n", org_id="o").autonomy_ceiling is None
    req = CreateApiKeyRequest(name="n", org_id="o", autonomy_ceiling="hard")
    assert req.autonomy_ceiling == "hard"
    with pytest.raises(ValidationError):
        CreateApiKeyRequest(name="n", org_id="o", autonomy_ceiling="unlimited")


def test_migration_chains_onto_a_single_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(_VERSIONS.parent))
    assert script.get_heads() == ["7a1f3c9e2b50"]
    assert script.get_revision("7a1f3c9e2b50").down_revision == "5e2d9c1a7b40"


# ── routes: who may set a ceiling, and what create/list return ─────────────


@pytest.fixture
def client(tmp_path):
    """The api_keys router on a per-test SQLite DB with one org.

    Users: ``admin-u`` (admin), ``member-u`` (member), ``viewer-u`` (viewer). The caller is chosen
    per request by the ``X-Test-Principal`` header, which a tiny middleware
    turns into ``request.state.user`` the way ``TokenAuthMiddleware`` does.
    """
    import asyncio

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.database.models import Base, Organization, OrgMember, User
    from server.routes import api_keys as api_keys_routes
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    session_local = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_local() as db:
            for uid in ("admin-u", "member-u", "viewer-u"):
                db.add(User(id=uid, email=f"{uid}@x.test", password_hash="x"))
            await db.flush()
            db.add(Organization(id="org-1", name="o", slug="o", owner_id="admin-u"))
            await db.flush()
            db.add(OrgMember(org_id="org-1", user_id="admin-u", role="admin"))
            db.add(OrgMember(org_id="org-1", user_id="member-u", role="member"))
            db.add(OrgMember(org_id="org-1", user_id="viewer-u", role="viewer"))
            await db.commit()

    asyncio.run(_init())

    app = FastAPI()
    app.include_router(api_keys_routes.router)

    @app.middleware("http")
    async def _principal(request: Request, call_next):
        uid, _, key_id = request.headers.get("X-Test-Principal", "").partition(":")
        request.state.user = {"id": uid}
        if key_id:
            request.state.user["api_key_id"] = key_id
        return await call_next(request)

    async def _db():
        async with session_local() as session:
            yield session

    async def _user(request: Request):
        return User(id=request.state.user["id"])

    # Key on the router module's own references: other tests reload server.*
    # modules, so a freshly imported get_db may not be the one it captured.
    app.dependency_overrides[api_keys_routes.get_db] = _db
    app.dependency_overrides[api_keys_routes.get_current_user] = _user
    with TestClient(app) as c:
        yield c
    asyncio.run(engine.dispose())


def _create(client, principal, **extra):
    return client.post(
        "/api/keys",
        json={"name": "k", "org_id": "org-1", **extra},
        headers={"X-Test-Principal": principal},
    )


def test_route_key_authenticated_caller_cannot_mint_a_key(client):
    assert _create(client, "admin-u:acw-key-1").status_code == 403


def test_route_member_cannot_set_a_ceiling(client):
    assert _create(client, "member-u", autonomy_ceiling="low").status_code == 403


def test_route_viewer_cannot_mint_write_scope(client):
    assert _create(client, "viewer-u", scopes=["mcp:write"]).status_code == 403


def test_route_viewer_cannot_mix_read_with_other_scopes(client):
    r = _create(client, "viewer-u", scopes=["mcp:read", "task:write"])
    assert r.status_code == 403


def test_route_viewer_can_mint_read_scope(client):
    assert _create(client, "viewer-u", scopes=["mcp:read"]).status_code == 201


def test_route_member_can_mint_write_scope(client):
    assert _create(client, "member-u", scopes=["mcp:write"]).status_code == 201


def test_route_member_without_ceiling_still_works(client):
    r = _create(client, "member-u")
    assert r.status_code == 201
    assert r.json()["autonomy_ceiling"] is None


def test_route_admin_sets_ceiling_and_create_and_list_return_it(client):
    r = _create(client, "admin-u", autonomy_ceiling="medium")
    assert r.status_code == 201
    body = r.json()
    assert body["autonomy_ceiling"] == "medium"
    assert body["agent_identity"] == f"agent:aifactory/{body['id']}"

    listed = client.get("/api/keys", headers={"X-Test-Principal": "admin-u"}).json()
    assert [(k["id"], k["autonomy_ceiling"], k["agent_identity"]) for k in listed] == [
        (body["id"], "medium", f"agent:aifactory/{body['id']}")
    ]
