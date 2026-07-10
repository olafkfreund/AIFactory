"""Tests for the dry-run TenantReconciler (Epic #35 #36 PR-1).

Covers the decision logic in isolation:
- Org without isolation toggle → 'no_op' / 'shared'.
- Org with isolation toggle but no namespace yet → 'create' with
  derived namespace name.
- Org with isolation toggle + namespace already set → 'no_op' steady state.
- Org soft-deleted within grace period → 'soft_delete_acked'.
- Org soft-deleted past grace period → 'tear_down'.
- Org with state drift (namespace set but mode != isolated) → 'update'.
- Failure path: a reconcile that raises records error + returns 'no_op'.
- ``derive_namespace_name`` returns the deterministic prefix-slug shape.

The reconciler is dry-run: no K8s/IAM/Vault calls. Tests assert on
the returned `ReconcileDecision` and the `tenant_states` row state.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))


from server.database.models import Base, Organization, TenantState, User
from server.services import tenant_reconciler as svc


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def fresh_db():
    """In-memory SQLite + Base.metadata.create_all. Mirrors
    tests/audit/conftest.py's pattern."""
    from sqlalchemy.ext.asyncio import (
        async_sessionmaker,
        create_async_engine,
    )

    nonce = secrets.token_hex(8)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///file:tenant-recon-{nonce}"
        f"?mode=memory&cache=shared&uri=true",
    )

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    _run(_init())
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    yield engine, SessionLocal


async def _seed_org(
    SessionLocal, *, slug: str = "acme", deleted_at=None
) -> Organization:
    """Create a User + Organization for a reconcile test."""
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
            deleted_at=deleted_at,
        )
        db.add(user)
        db.add(org)
        await db.commit()
        await db.refresh(org)
        return org


# ---------------------------------------------------------------------------
# derive_namespace_name — pure function
# ---------------------------------------------------------------------------


def test_derive_namespace_name_uses_prefix_and_slug():
    assert svc.derive_namespace_name("acme") == "aifactory-tenant-acme"
    assert svc.derive_namespace_name("corp-eu") == "aifactory-tenant-corp-eu"


def test_derive_namespace_name_respects_env_override(monkeypatch):
    """Operator may override the prefix via TENANT_NAMESPACE_PREFIX."""
    monkeypatch.setenv("TENANT_NAMESPACE_PREFIX", "myco")
    # The module reads at module load, so we have to reload to pick up.
    import importlib

    importlib.reload(svc)
    try:
        assert svc.derive_namespace_name("acme") == "myco-acme"
    finally:
        # Reset for other tests.
        monkeypatch.setenv("TENANT_NAMESPACE_PREFIX", "aifactory-tenant")
        importlib.reload(svc)


# ---------------------------------------------------------------------------
# reconcile_org — decision matrix
# ---------------------------------------------------------------------------


def test_no_op_when_isolation_disabled(fresh_db):
    """isolation_enabled=False → shared mode, no namespace."""
    _engine, SessionLocal = fresh_db
    org = _run(_seed_org(SessionLocal))

    async def _go():
        async with SessionLocal() as db:
            org_db = await db.get(Organization, org.id)
            return await svc.reconcile_org(
                db,
                org_db,
                isolation_enabled=False,
            )

    decision = _run(_go())
    assert decision.action == "no_op"
    assert decision.isolation_mode == "shared"
    assert decision.target_namespace is None


def test_create_decision_on_first_reconcile_under_isolation(fresh_db):
    """isolation_enabled=True + no prior state → 'create' with derived name."""
    _engine, SessionLocal = fresh_db
    org = _run(_seed_org(SessionLocal, slug="acme"))

    async def _go():
        async with SessionLocal() as db:
            org_db = await db.get(Organization, org.id)
            return await svc.reconcile_org(
                db,
                org_db,
                isolation_enabled=True,
            )

    decision = _run(_go())
    assert decision.action == "create"
    assert decision.isolation_mode == "isolated"
    assert decision.target_namespace == "aifactory-tenant-acme"
    assert "first reconcile" in decision.rationale


def test_no_op_steady_state_after_create(fresh_db):
    """A reconcile pass where state already shows isolated + namespace
    set returns 'no_op' steady state."""
    _engine, SessionLocal = fresh_db
    org = _run(_seed_org(SessionLocal, slug="acme"))

    async def _go():
        # First reconcile creates state row + would create namespace.
        # PR-2 will actually do the K8s create; for PR-1 we manually
        # set the state to simulate post-PR-2 steady state.
        async with SessionLocal() as db:
            org_db = await db.get(Organization, org.id)
            await svc.reconcile_org(db, org_db, isolation_enabled=True)
            state = await db.get(TenantState, org.id)
            state.namespace_name = "aifactory-tenant-acme"
            state.isolation_mode = "isolated"
            await db.commit()

        async with SessionLocal() as db:
            org_db = await db.get(Organization, org.id)
            return await svc.reconcile_org(
                db,
                org_db,
                isolation_enabled=True,
            )

    decision = _run(_go())
    assert decision.action == "no_op"
    assert decision.isolation_mode == "isolated"
    assert decision.target_namespace == "aifactory-tenant-acme"
    assert "steady state" in decision.rationale


def test_soft_delete_acked_within_grace_period(fresh_db):
    """Org soft-deleted within the grace window → 'soft_delete_acked'."""
    _engine, SessionLocal = fresh_db
    # Soft-deleted 5 days ago; default grace = 30 days.
    deleted_at = datetime.utcnow() - timedelta(days=5)
    org = _run(_seed_org(SessionLocal, deleted_at=deleted_at))

    async def _go():
        async with SessionLocal() as db:
            org_db = await db.get(Organization, org.id)
            return await svc.reconcile_org(
                db,
                org_db,
                isolation_enabled=True,
            )

    decision = _run(_go())
    assert decision.action == "soft_delete_acked"
    assert decision.isolation_mode == "deleted"
    assert "5 days ago" in decision.rationale
    assert "25 days until tear-down" in decision.rationale


def test_tear_down_decision_past_grace_period(fresh_db):
    """Soft-deleted 31 days ago → 'tear_down'."""
    _engine, SessionLocal = fresh_db
    deleted_at = datetime.utcnow() - timedelta(days=31)
    org = _run(_seed_org(SessionLocal, deleted_at=deleted_at))

    async def _go():
        async with SessionLocal() as db:
            org_db = await db.get(Organization, org.id)
            return await svc.reconcile_org(
                db,
                org_db,
                isolation_enabled=True,
            )

    decision = _run(_go())
    assert decision.action == "tear_down"
    assert decision.isolation_mode == "deleted"


def test_state_drift_triggers_update(fresh_db):
    """namespace_name set + isolation_mode='shared' = drift → 'update'."""
    _engine, SessionLocal = fresh_db
    org = _run(_seed_org(SessionLocal, slug="acme"))

    async def _go():
        async with SessionLocal() as db:
            # Manually create a drifted state row.
            state = TenantState(
                org_id=org.id,
                isolation_mode="shared",
                namespace_name="aifactory-tenant-acme",  # set, but mode wrong
            )
            db.add(state)
            await db.commit()

        async with SessionLocal() as db:
            org_db = await db.get(Organization, org.id)
            return await svc.reconcile_org(
                db,
                org_db,
                isolation_enabled=True,
            )

    decision = _run(_go())
    assert decision.action == "update"
    assert decision.isolation_mode == "isolated"
    assert "drift" in decision.rationale


def test_failure_path_records_error_returns_no_op(fresh_db, monkeypatch):
    """A reconcile that raises mid-computation records reconcile_error
    and returns 'no_op' rather than crashing."""
    _engine, SessionLocal = fresh_db
    org = _run(_seed_org(SessionLocal))

    # Inject a failure: monkeypatch _compute_decision to raise.
    def _bad(*args, **kwargs):
        raise RuntimeError("simulated reconcile failure")

    monkeypatch.setattr(svc, "_compute_decision", _bad)

    async def _go():
        async with SessionLocal() as db:
            org_db = await db.get(Organization, org.id)
            decision = await svc.reconcile_org(
                db,
                org_db,
                isolation_enabled=True,
            )
            await db.commit()
            return decision

    decision = _run(_go())
    assert decision.action == "no_op"
    assert "reconcile failed" in decision.rationale

    # The error is recorded in the state row.
    async def _check():
        async with SessionLocal() as db:
            state = await db.get(TenantState, org.id)
            return state.reconcile_error

    err = _run(_check())
    assert err is not None
    assert "RuntimeError" in err
    assert "simulated reconcile failure" in err


def test_state_drift_after_isolation_disabled(fresh_db):
    """Operator flipped isolation OFF after enabling it on an org
    that already had isolated state. Reconciler does NOT auto-tear-down;
    returns 'no_op' with a rationale documenting the operator action
    required."""
    _engine, SessionLocal = fresh_db
    org = _run(_seed_org(SessionLocal, slug="acme"))

    async def _go():
        # Pretend we previously reconciled under isolation.
        async with SessionLocal() as db:
            state = TenantState(
                org_id=org.id,
                isolation_mode="isolated",
                namespace_name="aifactory-tenant-acme",
            )
            db.add(state)
            await db.commit()

        # Now reconcile with isolation_enabled=False.
        async with SessionLocal() as db:
            org_db = await db.get(Organization, org.id)
            return await svc.reconcile_org(
                db,
                org_db,
                isolation_enabled=False,
            )

    decision = _run(_go())
    assert decision.action == "no_op"
    # The mode in the decision reflects the existing (non-shared) state.
    assert decision.isolation_mode == "isolated"
    assert "operator must explicitly migrate" in decision.rationale


# ---------------------------------------------------------------------------
# reconcile_all — sweep
# ---------------------------------------------------------------------------


def test_reconcile_all_processes_every_org(fresh_db):
    _engine, SessionLocal = fresh_db
    _run(_seed_org(SessionLocal, slug="a"))
    _run(_seed_org(SessionLocal, slug="b"))
    _run(_seed_org(SessionLocal, slug="c"))

    async def _go():
        async with SessionLocal() as db:
            return await svc.reconcile_all(db, isolation_enabled=True)

    decisions = _run(_go())
    assert len(decisions) == 3
    assert all(d.action == "create" for d in decisions)
    assert {d.target_namespace for d in decisions} == {
        "aifactory-tenant-a",
        "aifactory-tenant-b",
        "aifactory-tenant-c",
    }
