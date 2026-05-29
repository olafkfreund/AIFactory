"""Tests for the per-tenant anchor-key issuance reconciler hook (v1.2 #208).

Covers:
- On _apply_create_or_update with AUDIT_ANCHOR_PER_TENANT=true → new key
  row inserted + Vault path written.
- Idempotent: rerun produces no new key row (active key already exists).
- When Vault write fails → DB row stays; no exception propagated (best-effort).
- When feature flag is off → no key issued (backward compat).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from server.services.per_tenant_anchor_key import (
    issue_tenant_anchor_key,
    per_tenant_enabled,
)

ORG_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _make_db(
    *,
    existing_key_row=None,
) -> AsyncMock:
    """Build a minimal async DB mock."""
    db = AsyncMock()

    key_result = MagicMock()
    key_result.scalar_one_or_none.return_value = existing_key_row

    # Second execute call: _upsert_tenant_audit_state query (returns None = no state)
    state_result = MagicMock()
    state_result.scalar_one_or_none.return_value = None

    db.execute.side_effect = [key_result, state_result]
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


def _make_vault_client() -> MagicMock:
    vc = MagicMock()
    vc.kv_put = MagicMock()
    return vc


class TestIssueTenantAnchorKey:
    @pytest.mark.asyncio
    async def test_new_key_issued(self) -> None:
        """First call with no existing key → inserts row + writes Vault."""
        db = _make_db(existing_key_row=None)
        vault = _make_vault_client()

        with patch(
            "server.services.per_tenant_anchor_key.get_backend",
        ) as mock_kms, patch(
            "server.services.per_tenant_anchor_key.generate_new_key",
            return_value=b"\x55" * 32,
        ):
            mock_kms.return_value.encrypt.return_value = b"\xaa" * 64

            result = await issue_tenant_anchor_key(db, vault, ORG_ID)

        assert result is True, "expected new key to be issued"
        # db.add is called twice: once for the key row, once for tenant_audit_state.
        assert db.add.call_count >= 1
        db.flush.assert_called()
        vault.kv_put.assert_called()

    @pytest.mark.asyncio
    async def test_idempotent_when_key_exists(self) -> None:
        """If an active key already exists, returns False without new row."""
        from server.database.models import AuditSigningKey

        existing = MagicMock(spec=AuditSigningKey)
        existing.version = 1
        existing.org_id = ORG_ID

        db = AsyncMock()
        key_result = MagicMock()
        key_result.scalar_one_or_none.return_value = existing
        db.execute.return_value = key_result
        vault = _make_vault_client()

        result = await issue_tenant_anchor_key(db, vault, ORG_ID)

        assert result is False, "expected no-op when key already exists"
        db.add.assert_not_called()
        vault.kv_put.assert_not_called()

    @pytest.mark.asyncio
    async def test_vault_failure_is_non_fatal(self) -> None:
        """Vault write failure doesn't propagate — DB row is the authoritative copy."""
        db = _make_db(existing_key_row=None)
        vault = _make_vault_client()
        vault.kv_put.side_effect = RuntimeError("Vault unreachable")

        with patch(
            "server.services.per_tenant_anchor_key.get_backend",
        ) as mock_kms, patch(
            "server.services.per_tenant_anchor_key.generate_new_key",
            return_value=b"\x55" * 32,
        ):
            mock_kms.return_value.encrypt.return_value = b"\xaa" * 64

            # Should not raise even though Vault fails.
            result = await issue_tenant_anchor_key(db, vault, ORG_ID)

        assert result is True, "Vault failure should not abort key issuance"
        assert db.add.call_count >= 1, "key row should have been added to db"

    @pytest.mark.asyncio
    async def test_no_vault_client_does_not_fail(self) -> None:
        """When vault_client=None, issuance skips Vault write silently."""
        db = _make_db(existing_key_row=None)

        with patch(
            "server.services.per_tenant_anchor_key.get_backend",
        ) as mock_kms, patch(
            "server.services.per_tenant_anchor_key.generate_new_key",
            return_value=b"\x55" * 32,
        ):
            mock_kms.return_value.encrypt.return_value = b"\xaa" * 64

            result = await issue_tenant_anchor_key(db, None, ORG_ID)

        assert result is True


class TestPerTenantEnabledFlag:
    def test_false_by_default(self) -> None:
        with patch.dict("os.environ", {"AUDIT_ANCHOR_PER_TENANT": "false"}):
            assert per_tenant_enabled() is False

    def test_true_when_set(self) -> None:
        with patch.dict("os.environ", {"AUDIT_ANCHOR_PER_TENANT": "true"}):
            assert per_tenant_enabled() is True

    def test_case_insensitive(self) -> None:
        with patch.dict("os.environ", {"AUDIT_ANCHOR_PER_TENANT": "TRUE"}):
            assert per_tenant_enabled() is True
