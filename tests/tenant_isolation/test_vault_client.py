"""Tests for the VaultClient wrapper (Epic #35 #36 PR-2).

Covers:
  - Policy HCL text matches design §5 EXACTLY (read+list on
    aifactory/data/orgs/<uuid>/*; no metadata path).
  - create_tenant_kubernetes_role binds SA namespace + name + policy.
  - delete_tenant_policy_and_role is idempotent on missing entries.
  - Missing SA token file → VaultClientError (not crash).
  - Missing VAULT_ADDR → VaultClientError (not crash).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from server.services.vault_client import (
    VaultClient,
    VaultClientError,
    build_tenant_policy_hcl,
)

pytestmark = pytest.mark.tenant_isolation

_UUID = "11111111-2222-3333-4444-555555555555"


# ---------------------------------------------------------------------------
# Policy HCL shape
# ---------------------------------------------------------------------------


def test_build_tenant_policy_hcl_matches_design():
    """Exact design §5 text: read+list on data path, NO metadata path."""
    hcl = build_tenant_policy_hcl(_UUID)
    assert f'path "aifactory/data/orgs/{_UUID}/*"' in hcl
    assert 'capabilities = ["read", "list"]' in hcl
    # The metadata path is intentionally OMITTED — tenants must not
    # enumerate secret versions.
    assert "aifactory/metadata/" not in hcl


# ---------------------------------------------------------------------------
# Client construction + auth guards
# ---------------------------------------------------------------------------


def test_missing_vault_addr_raises_on_use():
    """Empty VAULT_ADDR makes any operation raise (not silently no-op)."""
    c = VaultClient(addr="")
    with pytest.raises(VaultClientError):
        c.create_tenant_policy(_UUID)


def test_missing_sa_token_raises_on_authenticate(tmp_path, monkeypatch):
    """When the SA token file is absent, authenticate raises a clean
    VaultClientError (with operator-actionable message)."""
    # Point the SA token constant at a non-existent path.
    from server.services import vault_client as vc
    monkeypatch.setattr(vc, "_SA_TOKEN_PATH", tmp_path / "does-not-exist")

    fake_hvac = MagicMock()
    fake_hvac.Client = MagicMock(return_value=MagicMock())

    with patch.dict("sys.modules", {"hvac": fake_hvac}):
        c = VaultClient(addr="http://vault:8200")
        with pytest.raises(VaultClientError) as exc:
            c.create_tenant_policy(_UUID)
    assert "SA token" in str(exc.value)


# ---------------------------------------------------------------------------
# create_tenant_policy + create_tenant_kubernetes_role
# ---------------------------------------------------------------------------


@pytest.fixture
def authed_client(tmp_path, monkeypatch):
    """Build a VaultClient pre-authenticated against a mock hvac.Client."""
    from server.services import vault_client as vc

    token_file = tmp_path / "token"
    token_file.write_text("fake-sa-jwt")
    monkeypatch.setattr(vc, "_SA_TOKEN_PATH", token_file)

    inner = MagicMock()
    inner.sys.create_or_update_policy = MagicMock()
    inner.sys.delete_policy = MagicMock()
    inner.auth.kubernetes.login = MagicMock()
    inner.auth.kubernetes.create_role = MagicMock()
    inner.auth.kubernetes.delete_role = MagicMock()

    fake_hvac = MagicMock()
    fake_hvac.Client = MagicMock(return_value=inner)

    with patch.dict("sys.modules", {"hvac": fake_hvac}):
        c = VaultClient(addr="http://vault:8200")
        # Force the lazy init by hitting one method.
        c._authenticate()
        yield c, inner


def test_create_tenant_policy_writes_design_hcl(authed_client):
    c, inner = authed_client
    name = c.create_tenant_policy(_UUID)
    assert name == f"aifactory-tenant-{_UUID}"
    inner.sys.create_or_update_policy.assert_called_once()
    args = inner.sys.create_or_update_policy.call_args.kwargs
    assert args["name"] == name
    assert f'path "aifactory/data/orgs/{_UUID}/*"' in args["policy"]


def test_create_tenant_kubernetes_role_binds_sa(authed_client):
    c, inner = authed_client
    c.create_tenant_kubernetes_role(
        org_uuid=_UUID,
        sa_namespace="aifactory-tenant-acme",
        sa_name="aifactory-tenant-acme-agent",
        policy_name=f"aifactory-tenant-{_UUID}",
    )
    inner.auth.kubernetes.create_role.assert_called_once()
    kw = inner.auth.kubernetes.create_role.call_args.kwargs
    assert kw["name"] == f"aifactory-tenant-{_UUID}"
    assert kw["bound_service_account_names"] == (
        ["aifactory-tenant-acme-agent"]
    )
    assert kw["bound_service_account_namespaces"] == (
        ["aifactory-tenant-acme"]
    )
    assert kw["policies"] == [f"aifactory-tenant-{_UUID}"]


def test_create_tenant_policy_wraps_errors(authed_client):
    c, inner = authed_client
    inner.sys.create_or_update_policy.side_effect = RuntimeError("vault down")
    with pytest.raises(VaultClientError):
        c.create_tenant_policy(_UUID)


# ---------------------------------------------------------------------------
# delete_tenant_policy_and_role — idempotent
# ---------------------------------------------------------------------------


def test_delete_tenant_policy_and_role_tolerates_missing(authed_client):
    """Both calls swallow exceptions (the policy + role may already be
    gone from a previous tear-down pass)."""
    c, inner = authed_client
    inner.auth.kubernetes.delete_role.side_effect = RuntimeError("missing")
    inner.sys.delete_policy.side_effect = RuntimeError("missing")
    # No raise.
    c.delete_tenant_policy_and_role(_UUID)
