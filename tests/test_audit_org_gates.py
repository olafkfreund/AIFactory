"""Access-review and audit-export org gates (AIFactory#1915)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

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
    from server.database.models import Base, Organization, OrgMember, User
    from server.routes import access_review as ar
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
            await db.commit()

    asyncio.run(_init())

    app = FastAPI()
    app.include_router(ar.router)

    async def _db():
        async with session_local() as session:
            yield session

    async def _user(request: Request):
        return User(id=request.headers["X-Test-Principal"])

    # Override on the modules' own references: other tests reload server.*.
    app.dependency_overrides[ar.get_db] = _db
    app.dependency_overrides[orgs.get_db] = _db
    app.dependency_overrides[orgs.get_current_user] = _user
    with TestClient(app) as c:
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
