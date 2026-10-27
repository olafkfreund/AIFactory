"""Minting an expiring ``acw_`` key works on Postgres (#1913).

``api_keys.expires_at`` is a naive ``DateTime``; asyncpg rejects an aware value
with "can't subtract offset-naive and offset-aware datetimes", which SQLite hides.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_WEB_SERVER_DIR = Path(__file__).resolve().parents[2] / "apps" / "web-server"
if str(_WEB_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER_DIR))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from server.database.engine import get_db  # noqa: E402
from server.database.models import (  # noqa: E402
    ApiKey,
    Base,
    Organization,
    OrgMember,
    User,
)
from server.mcp_remote.auth import (  # noqa: E402
    MCPAuthError,
    _hash_key,
    _lookup_by_digest,
)
from server.routes import api_keys as api_keys_routes  # noqa: E402
from server.routes.auth_routes import get_current_user  # noqa: E402
from sqlalchemy import text, update  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

pytestmark = [pytest.mark.postgres, pytest.mark.slow]

_SCHEMA = "apikey_expiry_pg"


@pytest.fixture
def env(test_postgres_url):
    # Own schema: the sibling tests read the alembic-migrated public schema.
    # NullPool: TestClient runs requests on another event loop than asyncio.run.
    engine = create_async_engine(
        test_postgres_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": _SCHEMA}},
    )
    session_local = async_sessionmaker(engine, expire_on_commit=False)

    async def _reset():
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {_SCHEMA}"))
            await conn.run_sync(Base.metadata.create_all)
        async with session_local() as s:
            s.add(User(id="u", email="u@x.test", password_hash="x"))
            await s.flush()
            s.add(Organization(id="org-1", name="o", slug="o", owner_id="u"))
            await s.flush()
            s.add(OrgMember(org_id="org-1", user_id="u", role="member"))
            await s.commit()

    asyncio.run(_reset())

    async def _db():
        async with session_local() as s:
            yield s

    app = FastAPI()
    app.include_router(api_keys_routes.router)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: User(id="u")

    def run(fn):
        async def _go():
            async with session_local() as s:
                result = await fn(s)
                await s.commit()
                return result

        return asyncio.run(_go())

    yield TestClient(app), run

    async def _drop():
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
        await engine.dispose()

    asyncio.run(_drop())


def test_mint_with_expiry_then_lookup(env):
    client, run = env
    r = client.post(
        "/api/keys",
        json={"name": "k", "org_id": "org-1", "scopes": ["api"], "expires_in_days": 1},
    )
    assert r.status_code == 201, r.text
    digest = _hash_key(r.json()["raw_key"])

    async def _lookup(s):
        return await _lookup_by_digest(s, digest)

    assert run(_lookup).scopes == {"api"}

    async def _expire(s):
        await s.execute(
            update(ApiKey).values(expires_at=datetime.now() - timedelta(days=1))
        )

    run(_expire)
    with pytest.raises(MCPAuthError, match="expired"):
        run(_lookup)
