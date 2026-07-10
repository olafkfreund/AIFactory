"""Tests for the reconciler's LiteLLM virtual-key lifecycle (Epic #35 #38 PR-2b).

Covers the design's implementation-plan PR-2 lifecycle:
- Org create → sync_virtual_key_on_create posts to /key/generate.
- Org soft-delete → sync_virtual_key_on_soft_delete disables key.
- Org hard-delete → sync_virtual_key_on_hard_delete deletes key.
- Drift sweep → creates missing keys, revokes orphans.
- Failure of the admin call logs WARNING but never crashes the
  reconciler (failure-safe; next tick retries).
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_SERVER = REPO_ROOT / "apps" / "web-server"
if str(WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(WEB_SERVER))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Fake admin client used by every test
# ---------------------------------------------------------------------------


class _FakeAdminClient:
    """Records calls; returns scripted values."""

    def __init__(self):
        self.created: list[dict] = []
        self.disabled: list[str] = []
        self.deleted: list[str] = []
        self.list_response: list[dict] = []
        self.raise_on: set[str] = set()

    async def create_virtual_key(self, org_id, allowed_models, **kwargs):
        if "create" in self.raise_on:
            raise RuntimeError("synthetic admin failure")
        self.created.append(
            {
                "org_id": org_id,
                "allowed_models": list(allowed_models),
            }
        )
        return f"sk-{org_id}"

    async def disable_virtual_key(self, org_id):
        if "disable" in self.raise_on:
            raise RuntimeError("synthetic admin failure")
        self.disabled.append(org_id)

    async def delete_virtual_key(self, org_id):
        if "delete" in self.raise_on:
            raise RuntimeError("synthetic admin failure")
        self.deleted.append(org_id)

    async def list_virtual_keys(self):
        if "list" in self.raise_on:
            raise RuntimeError("synthetic admin failure")
        return list(self.list_response)


# ---------------------------------------------------------------------------
# Fixture: in-memory DB + seeded org
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db():
    from server.database.models import Base
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    nonce = secrets.token_hex(8)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///file:recon-lifecycle-{nonce}"
        f"?mode=memory&cache=shared&uri=true",
    )

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    _run(_init())

    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    return engine, SessionLocal


async def _seed_org(SessionLocal, slug="acme", allowed_models=None, deleted_at=None):
    from server.database.models import Organization, User

    async with SessionLocal() as db:
        user = User(
            id=f"u-{slug}",
            email=f"{slug}@example.com",
            password_hash="x",
            role="user",
            is_active=True,
        )
        org = Organization(
            id=f"org-{slug}",
            name=slug.title(),
            slug=slug,
            owner_id=user.id,
            allowed_models=allowed_models or ["*"],
            deleted_at=deleted_at,
        )
        db.add(user)
        db.add(org)
        await db.commit()
        await db.refresh(org)
        return org


# ---------------------------------------------------------------------------
# Org create — sync_virtual_key_on_create
# ---------------------------------------------------------------------------


def test_create_calls_admin_with_org_allowlist(fresh_db):
    _engine, SessionLocal = fresh_db
    org = _run(_seed_org(SessionLocal, slug="acme", allowed_models=["claude-*"]))
    admin = _FakeAdminClient()

    from server.services.tenant_reconciler import sync_virtual_key_on_create

    key = _run(sync_virtual_key_on_create(org, admin))
    assert key == "sk-org-acme"
    assert admin.created == [
        {"org_id": "org-acme", "allowed_models": ["claude-*"]},
    ]


def test_create_failure_returns_none(fresh_db):
    _engine, SessionLocal = fresh_db
    org = _run(_seed_org(SessionLocal))
    admin = _FakeAdminClient()
    admin.raise_on.add("create")

    from server.services.tenant_reconciler import sync_virtual_key_on_create

    # Must NOT raise; returns None so the next tick retries.
    key = _run(sync_virtual_key_on_create(org, admin))
    assert key is None


# ---------------------------------------------------------------------------
# Org soft-delete — sync_virtual_key_on_soft_delete
# ---------------------------------------------------------------------------


def test_soft_delete_disables_key(fresh_db):
    _engine, SessionLocal = fresh_db
    org = _run(_seed_org(SessionLocal, slug="acme"))
    admin = _FakeAdminClient()

    from server.services.tenant_reconciler import sync_virtual_key_on_soft_delete

    ok = _run(sync_virtual_key_on_soft_delete(org, admin))
    assert ok is True
    assert admin.disabled == ["org-acme"]


def test_soft_delete_failure_returns_false(fresh_db):
    _engine, SessionLocal = fresh_db
    org = _run(_seed_org(SessionLocal))
    admin = _FakeAdminClient()
    admin.raise_on.add("disable")

    from server.services.tenant_reconciler import sync_virtual_key_on_soft_delete

    ok = _run(sync_virtual_key_on_soft_delete(org, admin))
    assert ok is False


# ---------------------------------------------------------------------------
# Org hard-delete — sync_virtual_key_on_hard_delete
# ---------------------------------------------------------------------------


def test_hard_delete_deletes_key(fresh_db):
    _engine, SessionLocal = fresh_db
    org = _run(_seed_org(SessionLocal, slug="acme"))
    admin = _FakeAdminClient()

    from server.services.tenant_reconciler import sync_virtual_key_on_hard_delete

    ok = _run(sync_virtual_key_on_hard_delete(org, admin))
    assert ok is True
    assert admin.deleted == ["org-acme"]


def test_hard_delete_failure_returns_false(fresh_db):
    _engine, SessionLocal = fresh_db
    org = _run(_seed_org(SessionLocal))
    admin = _FakeAdminClient()
    admin.raise_on.add("delete")

    from server.services.tenant_reconciler import sync_virtual_key_on_hard_delete

    ok = _run(sync_virtual_key_on_hard_delete(org, admin))
    assert ok is False


# ---------------------------------------------------------------------------
# Drift sweep — reconcile_virtual_keys_drift
# ---------------------------------------------------------------------------


def test_drift_sweep_creates_keys_for_orgs_without_one(fresh_db):
    _engine, SessionLocal = fresh_db
    _run(_seed_org(SessionLocal, slug="a"))
    _run(_seed_org(SessionLocal, slug="b"))

    admin = _FakeAdminClient()
    admin.list_response = []  # no keys on LiteLLM side

    from server.services.tenant_reconciler import reconcile_virtual_keys_drift

    async def _go():
        async with SessionLocal() as db:
            return await reconcile_virtual_keys_drift(db, admin)

    counts = _run(_go())
    assert counts["created"] == 2
    assert counts["revoked"] == 0
    assert counts["errors"] == 0
    assert {c["org_id"] for c in admin.created} == {"org-a", "org-b"}


def test_drift_sweep_revokes_orphan_keys(fresh_db):
    _engine, SessionLocal = fresh_db
    _run(_seed_org(SessionLocal, slug="a"))  # only this org exists

    admin = _FakeAdminClient()
    # LiteLLM has keys for 3 orgs (a stays, b + c are orphans).
    admin.list_response = [
        {"metadata": {"aifactory_org_id": "org-a"}},
        {"metadata": {"aifactory_org_id": "org-b"}},
        {"metadata": {"aifactory_org_id": "org-c"}},
    ]

    from server.services.tenant_reconciler import reconcile_virtual_keys_drift

    async def _go():
        async with SessionLocal() as db:
            return await reconcile_virtual_keys_drift(db, admin)

    counts = _run(_go())
    assert counts["created"] == 0
    assert counts["revoked"] == 2
    assert counts["errors"] == 0
    assert set(admin.deleted) == {"org-b", "org-c"}


def test_drift_sweep_no_op_when_in_sync(fresh_db):
    _engine, SessionLocal = fresh_db
    _run(_seed_org(SessionLocal, slug="a"))

    admin = _FakeAdminClient()
    admin.list_response = [
        {"metadata": {"aifactory_org_id": "org-a"}},
    ]

    from server.services.tenant_reconciler import reconcile_virtual_keys_drift

    async def _go():
        async with SessionLocal() as db:
            return await reconcile_virtual_keys_drift(db, admin)

    counts = _run(_go())
    assert counts == {"created": 0, "revoked": 0, "errors": 0}
    assert admin.created == []
    assert admin.deleted == []


def test_drift_sweep_list_failure_does_not_crash(fresh_db):
    _engine, SessionLocal = fresh_db
    _run(_seed_org(SessionLocal, slug="a"))

    admin = _FakeAdminClient()
    admin.raise_on.add("list")

    from server.services.tenant_reconciler import reconcile_virtual_keys_drift

    async def _go():
        async with SessionLocal() as db:
            return await reconcile_virtual_keys_drift(db, admin)

    counts = _run(_go())
    assert counts["errors"] == 1
    assert counts["created"] == 0
