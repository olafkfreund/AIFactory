#!/usr/bin/env python3
"""
rmux live-console authorization (#322 — epic #318)
==================================================

The console attach/stream path had no per-task authz: any authenticated user
could attach to any task's agent terminal and inject keystrokes (RCE). These
tests pin the fix — ``_authorize_console`` authorizes against the session's own
``project_id`` (not a client-supplied path prefix), so user B cannot reach user
A's console.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from fastapi import HTTPException  # noqa: E402
from server.rmux import bridge  # noqa: E402
from server.rmux.session import SessionState  # noqa: E402

ALICE = {"id": "alice", "email": "a@x", "role": "user"}
BOB = {"id": "bob", "email": "b@x", "role": "user"}
SERVICE = {"id": "default", "role": "admin", "is_service": True}


@pytest.fixture(autouse=True)
def _force_enforcement(monkeypatch):
    """Pin auth ON in both modules (CI runs with APP_DISABLE_AUTH=true)."""
    monkeypatch.setattr("server.routes.project_authz._auth_disabled", lambda: False)
    monkeypatch.setattr("server.rmux.bridge._auth_disabled", lambda: False)


@pytest.fixture
def db_factory():
    """In-memory async SQLite: alice is member of org-1; bob owns org-2."""
    from server.database.models import (
        Base,
        Organization,
        OrgMember,
        User,
    )
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    nonce = secrets.token_hex(8)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///file:rmuxauthz-{nonce}?mode=memory&cache=shared&uri=true"
    )
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as s:
            s.add_all(
                [
                    User(
                        id="alice",
                        email="a@x",
                        name="A",
                        password_hash="x",
                        role="user",
                        is_active=True,
                    ),
                    User(
                        id="bob",
                        email="b@x",
                        name="B",
                        password_hash="x",
                        role="user",
                        is_active=True,
                    ),
                    Organization(
                        id="org-1",
                        name="Org1",
                        slug="org1",
                        owner_id="alice",
                        plan="free",
                    ),
                    Organization(
                        id="org-2",
                        name="Org2",
                        slug="org2",
                        owner_id="bob",
                        plan="free",
                    ),
                    OrgMember(org_id="org-1", user_id="alice", role="member"),
                    OrgMember(org_id="org-2", user_id="bob", role="owner"),
                ]
            )
            await s.commit()

    import asyncio

    asyncio.run(_init())
    return SessionLocal


@pytest.fixture
def patch_projects(monkeypatch):
    # Project p1 is owned by org-1 (alice's org).
    monkeypatch.setattr(
        "server.routes.projects.load_projects",
        lambda: {"p1": {"org_id": "org-1", "name": "p1"}},
    )


def _state(project_id):
    return SessionState(
        spec_id="spec-x",
        session_name="s",
        fifo_path=Path("/tmp/x"),
        project_id=project_id,
    )


async def _authorize(user, project_id, SessionLocal, role="member"):
    async with SessionLocal() as db:
        return await bridge._authorize_console(
            user, _state(project_id), db, minimum_role=role
        )


class TestConsoleAuthz:
    @pytest.mark.asyncio
    async def test_member_of_owning_org_allowed(self, db_factory, patch_projects):
        org_id = await _authorize(ALICE, "p1", db_factory, role="member")
        assert org_id == "org-1"

    @pytest.mark.asyncio
    async def test_non_member_cannot_reach_other_users_console(
        self, db_factory, patch_projects
    ):
        # THE e2e property: bob (org-2) cannot attach to p1 (alice's org-1).
        with pytest.raises(HTTPException) as ei:
            await _authorize(BOB, "p1", db_factory, role="viewer")
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_service_principal_bypasses(self, db_factory, patch_projects):
        assert await _authorize(SERVICE, "p1", db_factory, role="member") is None

    @pytest.mark.asyncio
    async def test_unowned_session_is_service_only(self, db_factory):
        # project_id=None → only the service principal / auth-disabled may touch.
        with pytest.raises(HTTPException) as ei:
            await _authorize(ALICE, None, db_factory, role="viewer")
        assert ei.value.status_code == 403
        assert await _authorize(SERVICE, None, db_factory, role="viewer") is None

    @pytest.mark.asyncio
    async def test_authz_keys_on_session_project_not_path(
        self, db_factory, patch_projects
    ):
        # Even though bob owns *some* project, the session belongs to p1 (org-1);
        # authz keys on the session's project_id, so a borrowed prefix can't help.
        with pytest.raises(HTTPException):
            await _authorize(BOB, "p1", db_factory, role="member")
