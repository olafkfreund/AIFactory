"""A project-authz denial's audit row survives the request rollback (#1919).

The request session is never committed after a 403 (``get_db`` only closes it).
SQLite hides the loss: pysqlite emits no BEGIN, so a savepoint's RELEASE commits.
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
from server.services.audit_service import ACTION_AUTHZ_DENIED  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

pytestmark = [pytest.mark.postgres, pytest.mark.slow]

_SCHEMA = "authz_denial_pg"


def test_denial_row_survives_request_rollback(test_postgres_url, monkeypatch) -> None:
    engine = create_async_engine(
        test_postgres_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": _SCHEMA}},
    )
    session_local = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_engine, "async_session_factory", session_local)
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    monkeypatch.setattr(
        projects_mod,
        "load_projects",
        lambda: {"proj-1": {"id": "proj-1", "org_id": "org-x"}},
    )

    async def _go() -> list[AuditLog]:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {_SCHEMA}"))
            await conn.run_sync(Base.metadata.create_all)
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
            await s.rollback()

        try:
            async with session_local() as s:
                return list((await s.execute(select(AuditLog))).scalars())
        finally:
            # The downgrade tests look for leftover tables in every schema.
            async with engine.begin() as conn:
                await conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
            await engine.dispose()

    rows = asyncio.run(_go())
    assert [r.action for r in rows] == [ACTION_AUTHZ_DENIED]
    assert rows[0].resource_id == "proj-1"
    assert rows[0].user_id == "user-9"
