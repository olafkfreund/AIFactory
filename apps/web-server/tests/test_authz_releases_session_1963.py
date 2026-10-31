"""An allowed project check releases its request-scoped DB connection (#1963)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

_WEB_SERVER = Path(__file__).resolve().parents[1]
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.database.models import OrgMember  # noqa: E402
from server.routes import project_authz  # noqa: E402


@pytest.mark.asyncio
async def test_allowed_project_check_releases_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    monkeypatch.setattr(
        "server.project_registry.load_projects",
        lambda: {"p1": {"org_id": "org-1"}},
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(OrgMember.__table__.create)
        async with AsyncSession(engine) as db:
            db.add(OrgMember(org_id="org-1", user_id="alice", role="member"))
            await db.commit()

            org_id = await project_authz.authorize_project_for_user(
                {"id": "alice", "role": "user"}, "p1", db, "member"
            )

            assert org_id == "org-1"
            assert not db.in_transaction()
    finally:
        await engine.dispose()
