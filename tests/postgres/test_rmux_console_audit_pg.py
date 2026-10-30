"""rmux console attach/detach audit on Postgres (#1939).

The handlers never commit the request session and ``get_db`` only closes it,
so a row written on it is rolled back. SQLite hides this: pysqlite emits no
BEGIN, so a savepoint's RELEASE commits.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_WEB_SERVER_DIR = Path(__file__).resolve().parents[2] / "apps" / "web-server"
if str(_WEB_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER_DIR))

import server.project_registry as projects_mod  # noqa: E402
from server.database import engine as db_engine  # noqa: E402
from server.database.models import (  # noqa: E402
    AuditLog,
    Base,
    Organization,
    OrgMember,
    User,
)
from server.rmux import bridge  # noqa: E402
from server.rmux.session import SessionState  # noqa: E402
from server.routes import project_authz  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

pytestmark = [pytest.mark.postgres, pytest.mark.slow]

_SCHEMA = "rmux_console_audit_pg"


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


def test_attach_and_detach_rows_survive_request_rollback(
    session_local, monkeypatch
) -> None:
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    monkeypatch.setattr(
        projects_mod,
        "load_projects",
        lambda: {"proj-1": {"id": "proj-1", "org_id": "org-x"}},
    )
    state = SessionState(
        spec_id="spec-1",
        session_name="aifactory-task-spec-1",
        fifo_path=Path("/nonexistent"),
        project_id="proj-1",
    )
    monkeypatch.setattr(bridge, "_resolve_state_or_404", lambda _spec_id: state)
    request = SimpleNamespace(
        state=SimpleNamespace(user={"id": "user-1", "role": "user"}),
        client=SimpleNamespace(host="127.0.0.1"),
    )
    body = bridge.AttachRequest(connection_id="conn-1")

    async def _go() -> list[AuditLog]:
        async with session_local() as s:
            s.add(User(id="user-1", email="u@x.test", password_hash="x"))
            await s.flush()
            s.add(Organization(id="org-x", name="x", slug="x", owner_id="user-1"))
            await s.flush()
            s.add(OrgMember(org_id="org-x", user_id="user-1", role="member"))
            await s.commit()

        # One request session per call, closed without a commit like get_db.
        async with session_local() as s:
            assert (await bridge.attach("spec-1", body, request, s))[
                "status"
            ] == "attached"
            await s.rollback()
        async with session_local() as s:
            assert (await bridge.detach("spec-1", body, request, s))[
                "status"
            ] == "detached"
            await s.rollback()

        async with session_local() as s:
            result = await s.execute(select(AuditLog).order_by(AuditLog.created_at))
            return list(result.scalars())

    rows = asyncio.run(_go())
    assert [r.action for r in rows] == ["console.attach", "console.detach"]
    assert all(r.user_id == "user-1" and r.org_id == "org-x" for r in rows)
    assert all(r.resource_id == "spec-1" for r in rows)
