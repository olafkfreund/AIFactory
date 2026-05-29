"""Tests for the stage-2 tear-down entry point (Epic #35 #36 PR-2).

Covers:
  - Happy path: tear_down_org deletes K8s ns + S3 prefix + Vault +
    IAM in the right order; tenant_states.isolation_mode flips to
    'deleted'.
  - §4a safety: a non-UUID-shaped S3 prefix raises ValueError BEFORE
    any AWS call (the safety check is the regex inside the AWS
    client; tear_down_org lets the ValueError propagate so the
    operator sees the violation loud and clear).
  - dry_run=True lists + counts but does NOT delete K8s / Vault / IAM.
  - Missing tenant_state row → skip cleanly.
  - Leader-election: lock not acquired → skip cleanly.
"""

from __future__ import annotations

import asyncio
import importlib
import secrets
from unittest.mock import AsyncMock, MagicMock

import pytest
import server.services.tenant_reconciler as svc
from server.database.models import Base, Organization, TenantState, User

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
        f"sqlite+aiosqlite:///file:teardown-{nonce}"
        f"?mode=memory&cache=shared&uri=true",
    )

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    _run(_init())
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    yield engine, SessionLocal


async def _seed_isolated_org(SessionLocal):
    async with SessionLocal() as db:
        user = User(
            id="u-acme", email="acme@example.com",
            password_hash="x", role="user", is_active=True,
        )
        org = Organization(
            id="11111111-2222-3333-4444-555555555555",
            name="Acme", slug="acme", owner_id=user.id,
        )
        state = TenantState(
            org_id=org.id, isolation_mode="isolated",
            namespace_name="aifactory-tenant-acme",
            service_account="aifactory-tenant-acme-agent",
            iam_role_arn="arn:aws:iam::123:role/foo",
            vault_policy_name=f"aifactory-tenant-{org.id}",
        )
        db.add(user)
        db.add(org)
        db.add(state)
        await db.commit()
        return org


def _build_clients(*, s3_count: int = 5):
    k8s = MagicMock()
    k8s.delete_namespace = AsyncMock()
    aws = MagicMock()
    aws.delete_tenant_role = AsyncMock()
    aws.s3_recursive_delete = AsyncMock(return_value=s3_count)
    vault = MagicMock()
    vault.delete_tenant_policy_and_role = MagicMock()
    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)
    redis.eval = AsyncMock(return_value=1)
    return k8s, aws, vault, redis


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_tear_down_happy_path_deletes_everything(fresh_db, monkeypatch):
    _engine, SessionLocal = fresh_db
    monkeypatch.setenv("TENANT_S3_BUCKET", "aifactory-prod")
    importlib.reload(svc)

    org = _run(_seed_isolated_org(SessionLocal))
    k8s, aws, vault, redis = _build_clients(s3_count=7)

    async def _go():
        async with SessionLocal() as db:
            result = await svc.tear_down_org(
                db, org.id, dry_run=False,
                k8s_client=k8s, aws_client=aws, vault_client=vault,
                redis_client=redis,
            )
            await db.commit()
            return result

    result = _run(_go())
    assert result["k8s_deleted"] is True
    assert result["iam_deleted"] is True
    assert result["vault_deleted"] is True
    assert result["s3_objects_count"] == 7

    # K8s namespace was actually deleted.
    k8s.delete_namespace.assert_awaited_once_with("aifactory-tenant-acme")
    # S3 prefix matches design §4a shape.
    aws.s3_recursive_delete.assert_awaited_once()
    kw = aws.s3_recursive_delete.call_args.kwargs
    assert kw["prefix"] == f"orgs/{org.id}/"
    assert kw["dry_run"] is False
    # Vault policy + role deleted.
    vault.delete_tenant_policy_and_role.assert_called_once_with(org.id)
    # IAM role deleted.
    aws.delete_tenant_role.assert_awaited_once_with(org.id)

    # State flipped to 'deleted'.
    async def _check():
        async with SessionLocal() as db:
            return await db.get(TenantState, org.id)
    state = _run(_check())
    assert state.isolation_mode == "deleted"


# ---------------------------------------------------------------------------
# Dry run — listing only
# ---------------------------------------------------------------------------


def test_tear_down_dry_run_lists_but_does_not_delete(fresh_db, monkeypatch):
    """§7-stage-2's 24-hour dry-run pass: count + log, no deletes."""
    _engine, SessionLocal = fresh_db
    monkeypatch.setenv("TENANT_S3_BUCKET", "aifactory-prod")
    importlib.reload(svc)

    org = _run(_seed_isolated_org(SessionLocal))
    k8s, aws, vault, redis = _build_clients(s3_count=12)

    async def _go():
        async with SessionLocal() as db:
            return await svc.tear_down_org(
                db, org.id, dry_run=True,
                k8s_client=k8s, aws_client=aws, vault_client=vault,
                redis_client=redis,
            )

    result = _run(_go())
    assert result["dry_run"] is True
    assert result["s3_objects_count"] == 12
    # Listing fires (the AWS client itself decides whether to delete
    # based on its dry_run kwarg).
    aws.s3_recursive_delete.assert_awaited_once()
    # Everything else MUST be untouched.
    k8s.delete_namespace.assert_not_called()
    vault.delete_tenant_policy_and_role.assert_not_called()
    aws.delete_tenant_role.assert_not_called()


# ---------------------------------------------------------------------------
# §4a safety — propagated as ValueError
# ---------------------------------------------------------------------------


def test_tear_down_propagates_prefix_safety_violation(fresh_db, monkeypatch):
    """If the AWS client refuses a bad prefix, the ValueError MUST
    surface — never silently turned into a generic error."""
    _engine, SessionLocal = fresh_db
    monkeypatch.setenv("TENANT_S3_BUCKET", "aifactory-prod")
    importlib.reload(svc)

    org = _run(_seed_isolated_org(SessionLocal))
    k8s, aws, vault, redis = _build_clients()
    aws.s3_recursive_delete = AsyncMock(
        side_effect=ValueError("prefix must match ^orgs/[0-9a-f-]{36}/$"),
    )

    async def _go():
        async with SessionLocal() as db:
            return await svc.tear_down_org(
                db, org.id, dry_run=False,
                k8s_client=k8s, aws_client=aws, vault_client=vault,
                redis_client=redis,
            )

    with pytest.raises(ValueError):
        _run(_go())


# ---------------------------------------------------------------------------
# Skip cases
# ---------------------------------------------------------------------------


def test_tear_down_missing_org_skips(fresh_db):
    _engine, SessionLocal = fresh_db
    k8s, aws, vault, redis = _build_clients()

    async def _go():
        async with SessionLocal() as db:
            return await svc.tear_down_org(
                db, "nonexistent-org", dry_run=False,
                k8s_client=k8s, aws_client=aws, vault_client=vault,
                redis_client=redis,
            )

    result = _run(_go())
    assert result["status"] == "skipped"
    k8s.delete_namespace.assert_not_called()


def test_tear_down_missing_state_row_skips(fresh_db):
    """Org exists but has no tenant_states row → skip."""
    _engine, SessionLocal = fresh_db

    async def _seed():
        async with SessionLocal() as db:
            user = User(
                id="u-x", email="x@example.com",
                password_hash="x", role="user", is_active=True,
            )
            org = Organization(
                id="org-x", name="X", slug="x", owner_id=user.id,
            )
            db.add(user)
            db.add(org)
            await db.commit()

    _run(_seed())
    k8s, aws, vault, redis = _build_clients()

    async def _go():
        async with SessionLocal() as db:
            return await svc.tear_down_org(
                db, "org-x", dry_run=False,
                k8s_client=k8s, aws_client=aws, vault_client=vault,
                redis_client=redis,
            )

    result = _run(_go())
    assert result["status"] == "skipped"
    aws.s3_recursive_delete.assert_not_called()


def test_tear_down_lock_not_acquired_skips(fresh_db, monkeypatch):
    """Another replica holds the lock → we skip without acting."""
    _engine, SessionLocal = fresh_db
    monkeypatch.setenv("TENANT_S3_BUCKET", "aifactory-prod")
    importlib.reload(svc)

    org = _run(_seed_isolated_org(SessionLocal))
    k8s, aws, vault, _ = _build_clients()
    redis = MagicMock()
    redis.set = AsyncMock(return_value=False)  # lock denied
    redis.eval = AsyncMock(return_value=0)

    async def _go():
        async with SessionLocal() as db:
            return await svc.tear_down_org(
                db, org.id, dry_run=False,
                k8s_client=k8s, aws_client=aws, vault_client=vault,
                redis_client=redis,
            )

    result = _run(_go())
    assert result["status"] == "skipped"
    aws.s3_recursive_delete.assert_not_called()
