"""REST access with an ``acw_`` key needs the ``api`` scope and a live org (#1913)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from datetime import UTC, datetime  # noqa: E402

from server.auth import TokenAuthMiddleware  # noqa: E402
from server.database.engine import get_db  # noqa: E402
from server.database.models import (  # noqa: E402
    ApiKey,
    Base,
    Organization,
    OrgMember,
    User,
)
from server.routes.project_authz import require_project_access  # noqa: E402
from sqlalchemy import update  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

TOKEN = "acw_scopetest"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """App + SQLite DB with member ``u`` of org-1 and org-2 and one ``api`` key."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    session_local = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_local() as s:
            s.add(User(id="u", email="u@x.test", password_hash="x"))
            await s.flush()
            for n in (1, 2):
                s.add(
                    Organization(
                        id=f"org-{n}", name=f"o{n}", slug=f"o{n}", owner_id="u"
                    )
                )
            await s.flush()
            for n in (1, 2):
                s.add(OrgMember(org_id=f"org-{n}", user_id="u", role="member"))
            from server.mcp_remote.auth import _hash_key

            s.add(
                ApiKey(
                    user_id="u",
                    org_id="org-1",
                    key_hash=f"acw_xxxx${_hash_key(TOKEN)}",
                    name="k",
                    scopes="api",
                )
            )
            await s.commit()

    asyncio.run(_init())

    async def _db():
        async with session_local() as s:
            yield s

    monkeypatch.setattr("server.mcp_remote.auth.get_db", _db)
    monkeypatch.setattr(
        "server.auth.get_settings",
        lambda: SimpleNamespace(
            DISABLE_AUTH=False, API_TOKEN="legacy-xyz", JWT_SECRET="s"
        ),
    )
    monkeypatch.setattr("server.routes.project_authz._auth_disabled", lambda: False)
    monkeypatch.setattr("server.auth._try_decode_jwt", lambda token: None)
    monkeypatch.setattr("server.auth._is_legacy_api_token", lambda token: False)
    monkeypatch.setattr(
        "server.project_registry.load_projects",
        lambda: {"p2": {"org_id": "org-2"}},
    )

    app = FastAPI()
    app.add_middleware(TokenAuthMiddleware)
    app.dependency_overrides[get_db] = _db

    @app.get("/api/whoami")
    async def whoami():
        return {"ok": True}

    @app.get(
        "/api/projects/{project_id}/x", dependencies=[Depends(require_project_access())]
    )
    async def project_route(project_id: str):
        return {"ok": True}

    def _update(stmt):
        async def _go():
            async with session_local() as s:
                await s.execute(stmt)
                await s.commit()

        asyncio.run(_go())

    yield TestClient(app), _update
    asyncio.run(engine.dispose())


def test_api_scoped_member_key_gets_200(env):
    client, _ = env
    assert client.get("/api/whoami", headers=HEADERS).status_code == 200


def test_mcp_read_only_key_gets_401(env):
    client, update_db = env
    update_db(update(ApiKey).values(scopes="mcp:read"))
    assert client.get("/api/whoami", headers=HEADERS).status_code == 401


def test_empty_scope_key_gets_401(env):
    client, update_db = env
    update_db(update(ApiKey).values(scopes=""))
    assert client.get("/api/whoami", headers=HEADERS).status_code == 401


def test_deleted_org_key_gets_401(env):
    client, update_db = env
    update_db(update(Organization).values(deleted_at=datetime.now(UTC)))
    assert client.get("/api/whoami", headers=HEADERS).status_code == 401


def test_other_org_key_gets_403_on_project_route(env):
    client, _ = env
    assert client.get("/api/projects/p2/x", headers=HEADERS).status_code == 403
