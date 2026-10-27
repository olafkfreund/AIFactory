"""Git credential routes bind an ``acw_`` key to its own org (#1913).

The owner is a member of org-1 and org-2. A key minted for org-1 must not
create, list or delete PATs in org-2, even though the owner could.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

_WEB_SERVER_DIR = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER_DIR))

from server.database.models import Base, Organization, OrgMember, User  # noqa: E402
from server.routes import git_credentials as routes  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

_PAT = {"name": "n", "kind": "pat", "host": "github.com", "token": "t"}
_KEY_ORG1 = {"X-Key-Org": "org-1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Router on a per-test SQLite DB. ``X-Key-Org`` makes the caller an
    ``acw_`` key principal bound to that org; without it, the owner's JWT."""
    # The PAT column is encrypted at rest; any valid Fernet key will do here.
    monkeypatch.setenv("KMS_FERNET_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    session_local = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_local() as db:
            db.add(User(id="u", email="u@x.test", password_hash=""))
            await db.flush()
            for org in ("org-1", "org-2"):
                db.add(Organization(id=org, name=org, slug=org, owner_id="u"))
                await db.flush()
                db.add(OrgMember(org_id=org, user_id="u", role="admin"))
            await db.commit()

    asyncio.run(_init())

    app = FastAPI()
    app.include_router(routes.router)

    @app.middleware("http")
    async def _principal(request: Request, call_next):
        request.state.user = {"id": "u"}
        if key_org := request.headers.get("X-Key-Org"):
            request.state.user.update(api_key_id="k1", org_id=key_org)
        return await call_next(request)

    async def _db():
        async with session_local() as session:
            yield session

    app.dependency_overrides[routes.get_db] = _db
    app.dependency_overrides[routes.get_current_user] = lambda: User(id="u")
    with TestClient(app) as c:
        yield c
    asyncio.run(engine.dispose())


def _path(client) -> str:
    return next(r.path for r in client.app.routes if r.path.endswith("git-credentials"))


def test_key_cannot_create_in_another_org(client):
    r = client.post(_path(client), json={**_PAT, "org_id": "org-2"}, headers=_KEY_ORG1)
    assert r.status_code == 403


def test_key_cannot_list_another_org(client):
    r = client.get(_path(client), params={"org_id": "org-2"}, headers=_KEY_ORG1)
    assert r.status_code == 403


def test_key_cannot_delete_in_another_org(client):
    created = client.post(_path(client), json={**_PAT, "org_id": "org-2"})
    assert created.status_code == 201, created.text
    r = client.delete(f"{_path(client)}/{created.json()['id']}", headers=_KEY_ORG1)
    assert r.status_code == 403


def test_key_works_in_its_own_org(client):
    r = client.post(_path(client), json={**_PAT, "org_id": "org-1"}, headers=_KEY_ORG1)
    assert r.status_code == 201, r.text
    r = client.get(_path(client), params={"org_id": "org-1"}, headers=_KEY_ORG1)
    assert r.status_code == 200
    assert len(r.json()) == 1
