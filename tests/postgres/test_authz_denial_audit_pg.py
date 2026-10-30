"""Project-authz denial audit on Postgres (#1919).

The denial row survives the request rollback (``get_db`` never commits after a
403), the 403 does not hold the request's pooled connection while the audit
write takes a second one, and concurrent writers keep one linear hash chain.
SQLite hides all three: pysqlite emits no BEGIN, so a savepoint's RELEASE
commits, and it has a single writer.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_WEB_SERVER_DIR = Path(__file__).resolve().parents[2] / "apps" / "web-server"
if str(_WEB_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER_DIR))

import server.project_registry as projects_mod  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from server.database import engine as db_engine  # noqa: E402
from server.database.models import AuditLog, Base, Organization, User  # noqa: E402
from server.routes import project_authz  # noqa: E402
from server.services.audit_chain import row_as_mapping, verify_chain  # noqa: E402
from server.services.audit_service import (  # noqa: E402
    ACTION_AUTHZ_DENIED,
    log_audit_event_bg,
)
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

pytestmark = [pytest.mark.postgres, pytest.mark.slow]

_SCHEMA = "authz_denial_pg"


@pytest.fixture
def session_local(test_postgres_url, monkeypatch):
    engine = create_async_engine(
        test_postgres_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": _SCHEMA}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_engine, "async_session_factory", factory)

    async def _reset() -> None:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {_SCHEMA}"))
            await conn.run_sync(Base.metadata.create_all)

    async def _drop() -> None:
        # The downgrade tests look for leftover tables in every schema.
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
        await engine.dispose()

    asyncio.run(_reset())
    yield factory
    asyncio.run(_drop())


async def _chain(factory) -> list[dict]:
    async with factory() as s:
        result = await s.execute(select(AuditLog).order_by(AuditLog.created_at))
        return [row_as_mapping(r) for r in result.scalars()]


def test_denial_row_survives_request_rollback(session_local, monkeypatch) -> None:
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    monkeypatch.setattr(
        projects_mod,
        "load_projects",
        lambda: {"proj-1": {"id": "proj-1", "org_id": "org-x"}},
    )

    async def _go() -> list[dict]:
        async with session_local() as s:
            s.add(User(id="owner", email="o@x.test", password_hash="x"))
            s.add(User(id="user-9", email="u@x.test", password_hash="x"))
            await s.flush()
            s.add(Organization(id="org-x", name="x", slug="x", owner_id="owner"))
            await s.commit()

        # The request session: user-9 is not a member of org-x -> 403.
        async with session_local() as s:
            with pytest.raises(HTTPException) as exc:
                await project_authz.authorize_project_for_user(
                    {"id": "user-9", "role": "user"}, "proj-1", s
                )
            assert exc.value.status_code == 403
            # The membership SELECT's connection went back to the pool before
            # the audit write took its own (two per 403 starves the pool).
            assert not s.in_transaction()
            await s.rollback()

        return await _chain(session_local)

    rows = asyncio.run(_go())
    assert [r["action"] for r in rows] == [ACTION_AUTHZ_DENIED]
    assert rows[0]["resource_id"] == "proj-1"
    assert rows[0]["user_id"] == "user-9"


def test_concurrent_writers_keep_one_linear_chain(session_local) -> None:
    async def _go() -> list[dict]:
        await asyncio.gather(
            *(
                log_audit_event_bg(
                    action=ACTION_AUTHZ_DENIED,
                    resource_type="project",
                    resource_id=f"proj-{i}",
                )
                for i in range(20)
            )
        )
        return await _chain(session_local)

    rows = asyncio.run(_go())
    assert len(rows) == 20
    assert len({r["prev_hash"] for r in rows}) == 20, "two rows share a chain head"
    ok, bad, reason = verify_chain(rows)
    assert ok, f"chain broke at {bad}: {reason}"
