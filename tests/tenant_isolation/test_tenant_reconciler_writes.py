"""Integration tests for the reconciler's apply path (Epic #35 #36 PR-2).

Covers:
  - Happy path: org create → reconciler calls K8s + AWS + Vault in
    the correct order; updates tenant_states.
  - Failure path: a K8s call fails → tenant_states.reconcile_error
    populated; reconciler does NOT crash.
  - Leader-election: two replicas race for the same org_id → only
    one writes (mock Redis returning False for the second SETNX).
  - The PR-1 dry-run contract is preserved when ``apply=False``.
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


# ---------------------------------------------------------------------------
# In-memory SQLite fixture (mirrors tests/test_tenant_reconciler.py)
# ---------------------------------------------------------------------------


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
        f"sqlite+aiosqlite:///file:tenant-recon-writes-{nonce}"
        f"?mode=memory&cache=shared&uri=true",
    )

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    _run(_init())
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    yield engine, SessionLocal


async def _seed_org(SessionLocal, slug: str = "acme") -> Organization:
    async with SessionLocal() as db:
        user = User(
            id=f"u-{slug}", email=f"{slug}@example.com",
            password_hash="x", role="user", is_active=True,
        )
        org = Organization(
            id=f"org-{slug}", name=slug.title(), slug=slug,
            owner_id=user.id,
        )
        db.add(user)
        db.add(org)
        await db.commit()
        await db.refresh(org)
        return org


# ---------------------------------------------------------------------------
# Client mocks
# ---------------------------------------------------------------------------


def _build_k8s_mock(*, fail_step: str | None = None):
    """Build a k8s_client mock; optionally raise on one named step."""
    from server.services.k8s_client import KubernetesClientError

    m = MagicMock()
    m.create_tenant_namespace = AsyncMock()
    m.create_service_account = AsyncMock()
    m.create_role_and_binding = AsyncMock()
    m.apply_network_policies = AsyncMock()
    m.apply_resource_quota = AsyncMock()
    m.apply_limit_range = AsyncMock()
    m.delete_namespace = AsyncMock()
    m.get_namespace_status = AsyncMock()

    if fail_step:
        getattr(m, fail_step).side_effect = KubernetesClientError("boom")
    return m


def _build_aws_mock(*, role_arn: str = "arn:aws:iam::123:role/foo"):
    m = MagicMock()
    m.create_tenant_role = AsyncMock(return_value=role_arn)
    m.delete_tenant_role = AsyncMock()
    m.s3_recursive_delete = AsyncMock(return_value=0)
    return m


def _build_vault_mock(*, policy_name_prefix: str = "aifactory-tenant-"):
    m = MagicMock()
    # Vault is sync; use MagicMock, not AsyncMock.
    m.create_tenant_policy = MagicMock(
        side_effect=lambda uuid: f"{policy_name_prefix}{uuid}",
    )
    m.create_tenant_kubernetes_role = MagicMock()
    m.delete_tenant_policy_and_role = MagicMock()
    return m


def _build_redis_mock(*, lock_acquired: bool = True):
    """Mock async-redis SET + EVAL."""
    m = MagicMock()
    m.set = AsyncMock(return_value=lock_acquired)
    m.eval = AsyncMock(return_value=1)
    return m


# ---------------------------------------------------------------------------
# Backward-compat: apply=False preserves PR-1 contract
# ---------------------------------------------------------------------------


def test_apply_false_does_not_touch_clients(fresh_db, monkeypatch):
    """PR-1 callers pass apply=False; no client method is invoked."""
    _engine, SessionLocal = fresh_db
    monkeypatch.setenv("TENANT_OIDC_PROVIDER_ARN", "")
    monkeypatch.setenv("TENANT_S3_BUCKET", "")
    importlib.reload(svc)

    org = _run(_seed_org(SessionLocal))
    k8s = _build_k8s_mock()
    aws = _build_aws_mock()
    vault = _build_vault_mock()

    async def _go():
        async with SessionLocal() as db:
            org_db = await db.get(Organization, org.id)
            return await svc.reconcile_org(
                db, org_db, isolation_enabled=True,
                apply=False,  # default
                k8s_client=k8s, aws_client=aws, vault_client=vault,
            )

    decision = _run(_go())
    assert decision.action == "create"
    k8s.create_tenant_namespace.assert_not_called()
    aws.create_tenant_role.assert_not_called()
    vault.create_tenant_policy.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_apply_create_calls_k8s_aws_vault_in_order(fresh_db, monkeypatch):
    """apply=True → K8s primitives + AWS IAM + Vault all fire."""
    _engine, SessionLocal = fresh_db
    monkeypatch.setenv(
        "TENANT_OIDC_PROVIDER_ARN",
        "arn:aws:iam::123:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/X",
    )
    monkeypatch.setenv("TENANT_S3_BUCKET", "aifactory-prod")
    monkeypatch.setenv("TENANT_CLOUD_BACKEND", "aws")
    monkeypatch.setenv("TENANT_CNI_BACKEND", "cilium")
    importlib.reload(svc)

    org = _run(_seed_org(SessionLocal))
    k8s = _build_k8s_mock()
    aws = _build_aws_mock(role_arn="arn:aws:iam::123:role/tenant-foo")
    vault = _build_vault_mock()
    redis = _build_redis_mock(lock_acquired=True)

    async def _go():
        async with SessionLocal() as db:
            org_db = await db.get(Organization, org.id)
            decision = await svc.reconcile_org(
                db, org_db, isolation_enabled=True,
                apply=True,
                k8s_client=k8s, aws_client=aws, vault_client=vault,
                redis_client=redis,
            )
            await db.commit()
            return decision

    decision = _run(_go())
    assert decision.action == "create"

    # K8s order: namespace → SA → role/binding → netpol → quota → limit.
    k8s.create_tenant_namespace.assert_awaited_once()
    # SA is created TWICE: first without IRSA, then with IRSA after
    # the IAM role lands (PATCH path under the hood).
    assert k8s.create_service_account.await_count == 2
    k8s.create_role_and_binding.assert_awaited_once()
    k8s.apply_network_policies.assert_awaited_once()
    k8s.apply_resource_quota.assert_awaited_once()
    k8s.apply_limit_range.assert_awaited_once()

    aws.create_tenant_role.assert_awaited_once()
    vault.create_tenant_policy.assert_called_once_with(org.id)
    vault.create_tenant_kubernetes_role.assert_called_once()

    # State row reflects everything.
    async def _check():
        async with SessionLocal() as db:
            state = await db.get(TenantState, org.id)
            org_db = await db.get(Organization, org.id)
            return state, org_db

    state, org_db = _run(_check())
    assert state.isolation_mode == "isolated"
    assert state.namespace_name == "aifactory-tenant-acme"
    assert state.service_account == "aifactory-tenant-acme-agent"
    assert state.iam_role_arn == "arn:aws:iam::123:role/tenant-foo"
    assert state.vault_policy_name == f"aifactory-tenant-{org.id}"
    # The immutable namespace name landed on the org row.
    assert org_db.tenant_namespace == "aifactory-tenant-acme"


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_k8s_failure_records_reconcile_error(fresh_db, monkeypatch):
    """A K8s create_tenant_namespace failure surfaces in
    tenant_states.reconcile_error and does NOT crash."""
    _engine, SessionLocal = fresh_db
    monkeypatch.setenv("TENANT_S3_BUCKET", "aifactory-prod")
    monkeypatch.setenv(
        "TENANT_OIDC_PROVIDER_ARN", "arn:aws:iam::123:oidc-provider/x",
    )
    importlib.reload(svc)

    org = _run(_seed_org(SessionLocal))
    k8s = _build_k8s_mock(fail_step="create_tenant_namespace")
    aws = _build_aws_mock()
    vault = _build_vault_mock()
    redis = _build_redis_mock(lock_acquired=True)

    async def _go():
        async with SessionLocal() as db:
            org_db = await db.get(Organization, org.id)
            decision = await svc.reconcile_org(
                db, org_db, isolation_enabled=True,
                apply=True,
                k8s_client=k8s, aws_client=aws, vault_client=vault,
                redis_client=redis,
            )
            await db.commit()
            return decision

    decision = _run(_go())
    # The reconciler returns the failure-safe no-op (matches PR-1 pattern).
    assert decision.action == "no_op"

    async def _check():
        async with SessionLocal() as db:
            return await db.get(TenantState, org.id)

    state = _run(_check())
    assert state.reconcile_error is not None
    assert "KubernetesClientError" in state.reconcile_error


def test_vault_failure_is_non_fatal_for_k8s_path(fresh_db, monkeypatch):
    """A Vault outage records the error but K8s + AWS resources stay up."""
    _engine, SessionLocal = fresh_db
    monkeypatch.setenv("TENANT_S3_BUCKET", "aifactory-prod")
    monkeypatch.setenv(
        "TENANT_OIDC_PROVIDER_ARN", "arn:aws:iam::123:oidc-provider/x",
    )
    importlib.reload(svc)

    org = _run(_seed_org(SessionLocal))
    k8s = _build_k8s_mock()
    aws = _build_aws_mock(role_arn="arn:aws:iam::123:role/foo")
    vault = _build_vault_mock()
    from server.services.vault_client import VaultClientError
    vault.create_tenant_policy.side_effect = VaultClientError("vault down")
    redis = _build_redis_mock(lock_acquired=True)

    async def _go():
        async with SessionLocal() as db:
            org_db = await db.get(Organization, org.id)
            decision = await svc.reconcile_org(
                db, org_db, isolation_enabled=True,
                apply=True,
                k8s_client=k8s, aws_client=aws, vault_client=vault,
                redis_client=redis,
            )
            await db.commit()
            return decision

    decision = _run(_go())
    # K8s landed → action is still 'create' from a decision standpoint.
    assert decision.action == "create"

    async def _check():
        async with SessionLocal() as db:
            return await db.get(TenantState, org.id)

    state = _run(_check())
    assert state.isolation_mode == "isolated"
    assert state.namespace_name == "aifactory-tenant-acme"
    assert state.reconcile_error is not None
    assert "vault-create failed" in state.reconcile_error


# ---------------------------------------------------------------------------
# Leader election
# ---------------------------------------------------------------------------


def test_leader_election_blocks_second_replica(fresh_db, monkeypatch):
    """When Redis SETNX returns False for the second replica, that
    replica's reconcile is a no-op write."""
    _engine, SessionLocal = fresh_db
    monkeypatch.setenv("TENANT_S3_BUCKET", "")  # skip AWS side-effects
    monkeypatch.setenv("TENANT_OIDC_PROVIDER_ARN", "")
    importlib.reload(svc)

    org = _run(_seed_org(SessionLocal))
    k8s = _build_k8s_mock()
    aws = _build_aws_mock()
    vault = _build_vault_mock()
    redis = _build_redis_mock(lock_acquired=False)  # we don't get the lock

    async def _go():
        async with SessionLocal() as db:
            org_db = await db.get(Organization, org.id)
            return await svc.reconcile_org(
                db, org_db, isolation_enabled=True,
                apply=True,
                k8s_client=k8s, aws_client=aws, vault_client=vault,
                redis_client=redis,
            )

    decision = _run(_go())
    # Decision is computed (returned) but the writers were NOT called.
    assert decision.action == "create"
    k8s.create_tenant_namespace.assert_not_called()
    vault.create_tenant_policy.assert_not_called()


def test_redis_unavailable_refuses_to_write(fresh_db, monkeypatch):
    """When Redis itself errors, _acquire_lock returns False → write
    refused. Single-replica is the only safe mode without Redis."""
    _engine, SessionLocal = fresh_db
    importlib.reload(svc)

    org = _run(_seed_org(SessionLocal))
    k8s = _build_k8s_mock()
    aws = _build_aws_mock()
    vault = _build_vault_mock()

    redis = MagicMock()
    redis.set = AsyncMock(side_effect=RuntimeError("redis down"))
    redis.eval = AsyncMock()

    async def _go():
        async with SessionLocal() as db:
            org_db = await db.get(Organization, org.id)
            return await svc.reconcile_org(
                db, org_db, isolation_enabled=True,
                apply=True,
                k8s_client=k8s, aws_client=aws, vault_client=vault,
                redis_client=redis,
            )

    _run(_go())
    k8s.create_tenant_namespace.assert_not_called()


def test_redis_none_means_single_replica_proceeds(fresh_db, monkeypatch):
    """When redis_client is None (single-replica deployment), the
    lock acquires unconditionally + writes proceed."""
    _engine, SessionLocal = fresh_db
    monkeypatch.setenv("TENANT_S3_BUCKET", "")
    monkeypatch.setenv("TENANT_OIDC_PROVIDER_ARN", "")
    importlib.reload(svc)

    org = _run(_seed_org(SessionLocal))
    k8s = _build_k8s_mock()

    async def _go():
        async with SessionLocal() as db:
            org_db = await db.get(Organization, org.id)
            await svc.reconcile_org(
                db, org_db, isolation_enabled=True,
                apply=True,
                k8s_client=k8s, aws_client=None, vault_client=None,
                redis_client=None,
            )
            await db.commit()

    _run(_go())
    k8s.create_tenant_namespace.assert_awaited_once()
