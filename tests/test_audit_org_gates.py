"""Access-review and audit-export org gates (AIFactory#1915)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import Request

_WEB_SERVER_DIR = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER_DIR))


@pytest.fixture
def client(tmp_path):
    """Access-review router on SQLite: org A (admin-a, member-a), org B (admin-b)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.database.models import AuditLog, Base, Organization, OrgMember, User
    from server.routes import access_review as ar
    from server.routes import audit
    from server.routes import organizations as orgs
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    session_local = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_local() as db:
            for uid in ("admin-a", "member-a", "admin-b"):
                db.add(User(id=uid, email=f"{uid}@x.test", password_hash="x"))
            await db.flush()
            db.add(Organization(id="A", name="a", slug="a", owner_id="admin-a"))
            db.add(Organization(id="B", name="b", slug="b", owner_id="admin-b"))
            await db.flush()
            db.add(OrgMember(org_id="A", user_id="admin-a", role="admin"))
            db.add(OrgMember(org_id="A", user_id="member-a", role="member"))
            db.add(OrgMember(org_id="B", user_id="admin-b", role="admin"))
            db.add(OrgMember(org_id="B", user_id="admin-a", role="admin"))
            db.add(AuditLog(org_id="A", action="a.act", resource_type="t"))
            db.add(AuditLog(org_id="B", action="b.act", resource_type="t"))
            await db.commit()

    asyncio.run(_init())

    app = FastAPI()
    app.include_router(ar.router)
    app.include_router(audit.export_router)

    async def _db():
        async with session_local() as session:
            yield session

    async def _user(request: Request):
        return User(id=request.headers["X-Test-Principal"])

    # Override on the modules' own references: other tests reload server.*.
    app.dependency_overrides[ar.get_db] = _db
    app.dependency_overrides[orgs.get_db] = _db
    app.dependency_overrides[orgs.get_current_user] = _user
    app.dependency_overrides[audit.get_db] = _db
    app.dependency_overrides[audit.get_current_user] = _user

    @app.middleware("http")
    async def _principal(request: Request, call_next):
        request.state.user = {
            "id": request.headers["X-Test-Principal"],
            "is_service": "X-Test-Service" in request.headers,
        }
        if key_org := request.headers.get("X-Test-Key-Org"):
            request.state.user.update(api_key_id="k1", org_id=key_org)
        return await call_next(request)

    # Auth enabled, so only is_service_principal can open the unscoped export.
    with (
        patch("server.routes.project_authz._auth_disabled", return_value=False),
        TestClient(app) as c,
    ):
        yield c
    asyncio.run(engine.dispose())


def test_access_review_streams_only_the_gated_org(client):
    r = client.get(
        "/api/admin/access-review?org_id=A&org=B",
        headers={"X-Test-Principal": "admin-a"},
    )
    assert r.status_code == 200
    assert "admin-b@x.test" not in r.text
    assert "admin-a@x.test" in r.text
    assert "access-review-A.ndjson" in r.headers["content-disposition"]


def test_key_cannot_stream_another_orgs_roster(client):
    # admin-a is admin of A and B; a key bound to A must not read B's roster
    # through require_org_role (OrgRoleChecker's key-org binding, #1914).
    assert (
        client.get(
            "/api/admin/access-review?org_id=B", headers={"X-Test-Principal": "admin-a"}
        ).status_code
        == 200
    )
    r = client.get(
        "/api/admin/access-review?org_id=B",
        headers={"X-Test-Principal": "admin-a", "X-Test-Key-Org": "A"},
    )
    assert r.status_code == 403


def _export(client, principal, query="", **headers):
    return client.get(
        f"/api/audit/export{query}",
        headers={"X-Test-Principal": principal, **headers},
    )


def test_export_by_org_admin_returns_only_that_orgs_rows(client):
    r = _export(client, "admin-a", "?org_id=A")
    assert r.status_code == 200
    assert "a.act" in r.text
    assert "b.act" not in r.text


def test_export_by_org_member_is_forbidden(client):
    assert _export(client, "member-a", "?org_id=A").status_code == 403


def test_export_by_non_member_is_forbidden(client):
    assert _export(client, "admin-b", "?org_id=A").status_code == 403


def test_unscoped_export_by_human_is_forbidden(client):
    assert _export(client, "admin-a").status_code == 403


def test_unscoped_export_by_service_principal_succeeds(client):
    r = _export(client, "svc", **{"X-Test-Service": "1"})
    assert r.status_code == 200
    assert "a.act" in r.text
    assert "b.act" in r.text


def test_scoped_export_by_service_principal_succeeds(client):
    r = _export(client, "svc", "?org_id=B", **{"X-Test-Service": "1"})
    assert r.status_code == 200
    assert "b.act" in r.text


def test_key_cannot_export_another_org(client):
    # admin-a is admin in both orgs, but the key is bound to A.
    assert _export(client, "admin-a", "?org_id=B").status_code == 200
    r = _export(client, "admin-a", "?org_id=B", **{"X-Test-Key-Org": "A"})
    assert r.status_code == 403
