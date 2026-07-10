"""Tests for the agent spawner's tenant routing helper (Epic #35 #36 PR-2).

The legacy agent spawner targets the deployment-default namespace +
ServiceAccount. PR-2 adds ``resolve_tenant_target(db, org_id)`` so
pod-spawn callers can route into the per-tenant namespace when the
org is in ``isolation_mode='isolated'`` — and fall back to shared
mode otherwise.

Covers:
  - org with isolation_mode='isolated' + populated tenant_states →
    target namespace + SA returned.
  - org with isolation_mode='shared' (or no row) → shared fallback.
  - org with isolation_mode='deleted' → surfaced so the spawner can
    refuse new tasks (design §7 stage-1).
  - org_id=None (legacy single-tenant) → shared fallback.
  - DB exception → shared fallback + WARNING (failure-safe; spawner
    must never crash).
"""

from __future__ import annotations

import asyncio
import secrets
from unittest.mock import MagicMock

import pytest
from server.database.models import Base, Organization, TenantState, User
from server.services.agent_service import (
    TenantTarget,
    resolve_tenant_target,
)

pytestmark = pytest.mark.tenant_isolation


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def fresh_db():
    from sqlalchemy.ext.asyncio import (
        async_sessionmaker,
        create_async_engine,
    )

    nonce = secrets.token_hex(8)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///file:tenant-spawner-{nonce}"
        f"?mode=memory&cache=shared&uri=true",
    )

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    _run(_init())
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    yield engine, SessionLocal


async def _seed_org_with_state(
    SessionLocal,
    *,
    slug: str,
    isolation_mode: str,
    namespace: str | None = None,
    service_account: str | None = None,
) -> Organization:
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
        )
        db.add(user)
        db.add(org)
        await db.commit()
        await db.refresh(org)

        state = TenantState(
            org_id=org.id,
            isolation_mode=isolation_mode,
            namespace_name=namespace,
            service_account=service_account,
        )
        db.add(state)
        await db.commit()
        return org


# ---------------------------------------------------------------------------
# Resolution paths
# ---------------------------------------------------------------------------


def test_isolated_org_returns_per_tenant_namespace(fresh_db):
    _engine, SessionLocal = fresh_db
    org = _run(
        _seed_org_with_state(
            SessionLocal,
            slug="acme",
            isolation_mode="isolated",
            namespace="aifactory-tenant-acme",
            service_account="aifactory-tenant-acme-agent",
        )
    )

    async def _go():
        async with SessionLocal() as db:
            return await resolve_tenant_target(db, org.id)

    target = _run(_go())
    assert isinstance(target, TenantTarget)
    assert target.isolation_mode == "isolated"
    assert target.namespace == "aifactory-tenant-acme"
    assert target.service_account == "aifactory-tenant-acme-agent"


def test_shared_mode_returns_none_namespace(fresh_db):
    _engine, SessionLocal = fresh_db
    org = _run(
        _seed_org_with_state(
            SessionLocal,
            slug="legacy",
            isolation_mode="shared",
        )
    )

    async def _go():
        async with SessionLocal() as db:
            return await resolve_tenant_target(db, org.id)

    target = _run(_go())
    assert target.isolation_mode == "shared"
    assert target.namespace is None
    assert target.service_account is None


def test_no_state_row_returns_shared_fallback(fresh_db):
    """Org exists but has no tenant_states row (pre-#36 deployment)."""
    _engine, SessionLocal = fresh_db

    async def _seed():
        async with SessionLocal() as db:
            user = User(
                id="u-x",
                email="x@example.com",
                password_hash="x",
                role="user",
                is_active=True,
            )
            org = Organization(
                id="org-x",
                name="X",
                slug="x",
                owner_id=user.id,
            )
            db.add(user)
            db.add(org)
            await db.commit()
            return org

    _run(_seed())

    async def _go():
        async with SessionLocal() as db:
            return await resolve_tenant_target(db, "org-x")

    target = _run(_go())
    assert target.isolation_mode == "shared"
    assert target.namespace is None


def test_deleted_mode_surfaced_for_spawner_refusal(fresh_db):
    """Soft-deleted orgs must be visible to the spawner so it refuses
    new tasks (design §7 stage-1)."""
    _engine, SessionLocal = fresh_db
    org = _run(
        _seed_org_with_state(
            SessionLocal,
            slug="gone",
            isolation_mode="deleted",
            namespace="aifactory-tenant-gone",
            service_account="aifactory-tenant-gone-agent",
        )
    )

    async def _go():
        async with SessionLocal() as db:
            return await resolve_tenant_target(db, org.id)

    target = _run(_go())
    assert target.isolation_mode == "deleted"
    # The namespace is still surfaced — the spawner uses isolation_mode
    # to decide policy, not the namespace presence.
    assert target.namespace == "aifactory-tenant-gone"


def test_none_org_id_returns_shared():
    """Legacy single-tenant deployments may pass org_id=None."""

    async def _go():
        # Even a None db is fine: we never touch it when org_id is None.
        return await resolve_tenant_target(None, None)

    target = _run(_go())
    assert target.isolation_mode == "shared"
    assert target.namespace is None


def test_db_error_falls_back_to_shared():
    """ANY exception on the lookup → shared fallback. The spawner
    must never crash because the tenant_state row couldn't be read."""
    from unittest.mock import AsyncMock

    bad_db = MagicMock()
    bad_db.get = AsyncMock(side_effect=RuntimeError("db down"))

    async def _go():
        return await resolve_tenant_target(bad_db, "org-xyz")

    target = _run(_go())
    assert target.isolation_mode == "shared"
    assert target.namespace is None
