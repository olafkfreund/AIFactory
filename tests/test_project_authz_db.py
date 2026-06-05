"""DB-backed tests for project authorization enforcement (epic #318, #319 slice 2).

Exercises the ``ProjectAccessChecker`` dependency + ``accessible_org_ids``
against a real in-memory SQLite with seeded users/orgs/memberships — proving
tenant A cannot reach tenant B's project, role levels are enforced, and the
service principal still bypasses. Mirrors tests/audit's fresh-DB pattern.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.routes.project_authz import (  # noqa: E402
    ProjectAccessChecker,
    TaskAccessChecker,
    accessible_org_ids,
)


@pytest.fixture(autouse=True)
def _force_enforcement(monkeypatch):
    """These tests exercise enforcement, so pin auth ON regardless of the
    ambient APP_DISABLE_AUTH (CI runs the suite with DISABLE_AUTH=true)."""
    monkeypatch.setattr("server.routes.project_authz._auth_disabled", lambda: False)


@pytest.fixture
def db_factory():
    """In-memory async SQLite seeded with two tenants. Returns a session factory.

    - alice: ``member`` of org-1
    - bob:   ``owner`` of org-2
    """
    from server.database.models import (
        Base,
        Organization,
        OrgMember,
        User,
    )
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    nonce = secrets.token_hex(8)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///file:authz-{nonce}?mode=memory&cache=shared&uri=true"
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
                        id="org-1", name="O1", slug="o1", owner_id="alice", plan="free"
                    ),
                    Organization(
                        id="org-2", name="O2", slug="o2", owner_id="bob", plan="free"
                    ),
                    OrgMember(org_id="org-1", user_id="alice", role="member"),
                    OrgMember(org_id="org-2", user_id="bob", role="owner"),
                ]
            )
            await s.commit()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_init())
    finally:
        loop.close()
    return SessionLocal


def _req(user):
    return SimpleNamespace(state=SimpleNamespace(user=user))


# project p1 belongs to org-1 (alice's org).
def _patch_projects(monkeypatch):
    monkeypatch.setattr(
        "server.routes.projects.load_projects",
        lambda: {"p1": {"org_id": "org-1", "name": "p1"}},
    )


async def test_member_of_owning_org_allowed(db_factory, monkeypatch):
    _patch_projects(monkeypatch)
    async with db_factory() as s:
        res = await ProjectAccessChecker("member")(
            project_id="p1", request=_req({"id": "alice", "role": "user"}), db=s
        )
    assert res["id"] == "alice"


async def test_cross_tenant_access_is_403(db_factory, monkeypatch):
    # bob (org-2) must not reach p1 (org-1).
    _patch_projects(monkeypatch)
    async with db_factory() as s:
        with pytest.raises(HTTPException) as exc:
            await ProjectAccessChecker("viewer")(
                project_id="p1", request=_req({"id": "bob", "role": "user"}), db=s
            )
    assert exc.value.status_code == 403


async def test_insufficient_role_is_403(db_factory, monkeypatch):
    # alice is only 'member'; an 'admin' action is denied.
    _patch_projects(monkeypatch)
    async with db_factory() as s:
        with pytest.raises(HTTPException) as exc:
            await ProjectAccessChecker("admin")(
                project_id="p1", request=_req({"id": "alice", "role": "user"}), db=s
            )
    assert exc.value.status_code == 403


async def test_service_principal_bypasses_db(db_factory):
    # The service principal (siblings / local UI) is allowed without membership
    # — even for a project that doesn't exist.
    async with db_factory() as s:
        res = await ProjectAccessChecker("admin")(
            project_id="does-not-exist", request=_req({"is_service": True}), db=s
        )
    assert res["is_service"] is True


# ── task-scoped variant (task_id = "project_id:spec_id") ───────────────────


async def test_task_access_member_allowed(db_factory, monkeypatch):
    _patch_projects(monkeypatch)
    async with db_factory() as s:
        res = await TaskAccessChecker("member")(
            task_id="p1:001-x", request=_req({"id": "alice", "role": "user"}), db=s
        )
    assert res["id"] == "alice"


async def test_task_access_cross_tenant_403(db_factory, monkeypatch):
    _patch_projects(monkeypatch)
    async with db_factory() as s:
        with pytest.raises(HTTPException) as exc:
            await TaskAccessChecker("viewer")(
                task_id="p1:001-x", request=_req({"id": "bob", "role": "user"}), db=s
            )
    assert exc.value.status_code == 403


async def test_task_access_service_principal_bypasses(db_factory):
    # The sibling-contract guard: a service token (PFactory/TFactory, local UI)
    # reaches task routes (start / create-and-run / apply-correction) regardless
    # of org membership — proving #319 enforcement doesn't break M2M.
    async with db_factory() as s:
        res = await TaskAccessChecker("member")(
            task_id="anyproj:anyspec", request=_req({"is_service": True}), db=s
        )
    assert res["is_service"] is True


async def test_accessible_org_ids_scopes_humans_but_not_service(db_factory):
    async with db_factory() as s:
        alice = await accessible_org_ids(_req({"id": "alice", "role": "user"}), s)
        assert alice == {"org-1"}
        bob = await accessible_org_ids(_req({"id": "bob", "role": "user"}), s)
        assert bob == {"org-2"}
        service = await accessible_org_ids(_req({"is_service": True}), s)
        assert service is None  # sees everything
