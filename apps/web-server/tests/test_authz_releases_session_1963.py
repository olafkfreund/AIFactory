"""An allowed project check releases its request-scoped DB connection (#1963)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

_WEB_SERVER = Path(__file__).resolve().parents[1]
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.database.models import OrgMember, Team, TeamMembership  # noqa: E402
from server.routes import project_authz  # noqa: E402


@pytest.mark.asyncio
async def test_allowed_project_check_releases_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    monkeypatch.setattr(
        "server.project_registry.aload_projects",
        AsyncMock(
            return_value={
                "p1": {"org_id": "org-1", "team_id": "team-1"},
                "p2": {"org_id": "org-1", "team_id": "team-2"},
            }
        ),
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(OrgMember.__table__.create)
            await connection.run_sync(Team.__table__.create)
            await connection.run_sync(TeamMembership.__table__.create)
        async with AsyncSession(engine) as db:
            db.add(OrgMember(org_id="org-1", user_id="alice", role="member"))
            db.add(
                Team(
                    id="team-1",
                    org_id="org-1",
                    name="Default",
                    slug="default",
                    is_default=True,
                )
            )
            db.add(TeamMembership(team_id="team-1", user_id="alice", role="member"))
            db.add(
                Team(
                    id="team-2",
                    org_id="org-1",
                    name="Other",
                    slug="other",
                    is_default=False,
                )
            )
            await db.commit()

            org_id = await project_authz.authorize_project_for_user(
                {"id": "alice", "role": "user"}, "p1", db, "member"
            )

            assert org_id == "org-1"
            assert not db.in_transaction()

            with pytest.raises(HTTPException) as denied:
                await project_authz.require_project_role(
                    {"id": "alice", "scopes": ["mcp:read"]}, "p1", "member", db
                )
            assert denied.value.status_code == 403
            assert not db.in_transaction()

            with pytest.raises(HTTPException) as hidden:
                await project_authz.require_project_role(
                    {"id": "bob"}, "p1", "viewer", db
                )
            assert hidden.value.status_code == 404
            assert not db.in_transaction()

            service = {
                "id": "service",
                "org_id": "org-1",
                "scoped_service": True,
                "scopes": ["api"],
            }
            assert (
                await project_authz.require_project_role(service, "p1", "member", db)
            ).role == "member"
            with pytest.raises(HTTPException) as cross_team:
                await project_authz.require_project_role(service, "p2", "viewer", db)
            assert cross_team.value.status_code == 404

            await db.execute(delete(TeamMembership))
            await db.commit()
            assert (
                await project_authz.require_project_role(
                    {"id": "alice"}, "p1", "member", db
                )
            ).role == "member"

            monkeypatch.setattr(
                project_authz,
                "get_settings",
                lambda: SimpleNamespace(LEGACY_SHARED_TOKEN_FULL_ACCESS=False),
            )
            with pytest.raises(HTTPException) as no_bypass:
                await project_authz.require_project_role(
                    {"id": "default", "is_legacy_shared_token": True},
                    "p1",
                    "viewer",
                    db,
                )
            assert no_bypass.value.status_code == 404
    finally:
        await engine.dispose()
