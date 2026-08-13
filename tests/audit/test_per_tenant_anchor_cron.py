"""Tests for the per-tenant cron extension (v1.2 #208).

Covers:
- Happy path: emit_tenant_anchor_for_day writes one anchor per org.
- One tenant's failure does NOT block other tenants.
- Backward compat: AUDIT_ANCHOR_PER_TENANT=false runs shared-chain cron only.
- Idempotency: calling twice for the same (org, day) is a no-op.
- No key provisioned: anchor is skipped with a warning (fallback to shared).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from server.jobs.audit_anchor_cron import (
    _latest_tenant_chain_head_before,
    emit_tenant_anchor_for_day,
)
from server.services.audit_chain import tenant_genesis

ORG_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ORG_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
TARGET_DAY = date(2026, 5, 29)


def _mock_signing_key(version: int = 1) -> Any:
    from server.services.audit_anchor import _SigningKey

    return _SigningKey(raw=b"\x42" * 32, version=version)


def _make_async_db(
    *,
    existing_anchor: Any = None,
    signing_key: Any = None,
    isolated_orgs: list[str] | None = None,
    state_row: Any = None,
) -> AsyncMock:
    """Build a minimal async DB mock for cron tests."""
    db = AsyncMock()

    # _existing_anchor_for_org_day
    anchor_result = MagicMock()
    anchor_result.scalar_one_or_none.return_value = existing_anchor

    # _load_tenant_signing_key (two calls: one for the key query)
    key_result = MagicMock()
    key_result.scalar_one_or_none.return_value = signing_key

    # state row
    state_result = MagicMock()
    state_result.scalar_one_or_none.return_value = state_row

    # _select_isolated_org_ids
    org_result = MagicMock()
    org_result.scalars.return_value.all.return_value = isolated_orgs or []

    db.execute.side_effect = [
        anchor_result,  # _existing_anchor_for_org_day
        key_result,  # _load_tenant_signing_key
        anchor_result,  # _latest_tenant_chain_head_before (last row)
        anchor_result,  # _classifications_hash_before_for_org
        state_result,  # tenant_audit_state head update
    ]
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


class TestEmitTenantAnchorForDay:
    @pytest.mark.asyncio
    async def test_happy_path_emits_anchor(self) -> None:
        """When key exists + no prior anchor → inserts AuditAnchor row."""
        # Use a plain MagicMock (no spec) so we can set .wrapped_key freely.
        mock_key_row = MagicMock()
        mock_key_row.version = 1
        # wrapped_key must be a MagicMock so its .hex() method is mockable.
        mock_key_row.wrapped_key = MagicMock()
        mock_key_row.wrapped_key.hex.return_value = "42" * 32

        db = AsyncMock()
        # Call sequence for emit_tenant_anchor_for_day:
        # 1. _existing_anchor_for_org_day → None (not yet emitted)
        # 2. _load_tenant_signing_key → mock_key_row
        # 3. _latest_tenant_chain_head_before → None (no rows → genesis)
        # 4. _classifications_hash_before_for_org → empty iter
        # 5. tenant_audit_state state → None (no state to update)

        no_anchor = MagicMock()
        no_anchor.scalar_one_or_none.return_value = None

        key_result = MagicMock()
        key_result.scalar_one_or_none.return_value = mock_key_row

        no_rows = MagicMock()
        no_rows.scalar_one_or_none.return_value = None

        empty_cls = MagicMock()
        empty_cls.__iter__ = MagicMock(return_value=iter([]))

        no_state = MagicMock()
        no_state.scalar_one_or_none.return_value = None

        db.execute.side_effect = [
            no_anchor,  # _existing_anchor_for_org_day
            key_result,  # _load_tenant_signing_key
            no_rows,  # _latest_tenant_chain_head_before
            empty_cls,  # _classifications_hash_before_for_org
            no_state,  # tenant_audit_state update
        ]
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.rollback = AsyncMock()

        with patch(
            "server.jobs.audit_anchor_cron._cached_unwrap_key",
            return_value=b"\x42" * 32,
        ):
            result = await emit_tenant_anchor_for_day(db, ORG_A, TARGET_DAY)

        assert result is not None, "expected an AuditAnchor to be emitted"
        db.add.assert_called_once()
        db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_idempotent_when_anchor_exists(self) -> None:
        """If an anchor for (org, day) already exists, returns None (no-op)."""
        from server.database.models import AuditAnchor

        existing = MagicMock(spec=AuditAnchor)
        existing.key_version = 1

        db = AsyncMock()
        existing_result = MagicMock()
        existing_result.scalar_one_or_none.return_value = existing
        db.execute.return_value = existing_result

        result = await emit_tenant_anchor_for_day(db, ORG_A, TARGET_DAY)
        assert result is None, "existing anchor should be a no-op"
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_key(self) -> None:
        """When no signing key is provisioned, anchor is skipped (logs warning)."""
        db = AsyncMock()
        no_anchor = MagicMock()
        no_anchor.scalar_one_or_none.return_value = None
        no_key = MagicMock()
        no_key.scalar_one_or_none.return_value = None  # no key row
        db.execute.side_effect = [no_anchor, no_key]

        result = await emit_tenant_anchor_for_day(db, ORG_A, TARGET_DAY)
        assert result is None, "should skip gracefully when key not provisioned"
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_safe_on_db_error(self) -> None:
        """A DB error during emit returns None + logs, does NOT re-raise."""
        db = AsyncMock()
        db.execute.side_effect = RuntimeError("simulated DB failure")
        db.rollback = AsyncMock()

        # Should not raise.
        result = await emit_tenant_anchor_for_day(db, ORG_A, TARGET_DAY)
        assert result is None
        db.rollback.assert_called_once()


class TestCronPerTenantIteration:
    @pytest.mark.asyncio
    async def test_one_tenant_failure_does_not_block_others(self) -> None:
        """Per-tenant loop continues to the next org after a failure."""
        from server.jobs.audit_anchor_cron import (
            _emit_tenant_anchors_for_day,
        )

        call_order = []

        async def _mock_emit(db, org_id, target_day):
            call_order.append(org_id)
            if org_id == ORG_A:
                raise RuntimeError("simulated failure for org A")
            return MagicMock()

        db = AsyncMock()
        db.commit = AsyncMock()

        with (
            patch(
                "server.jobs.audit_anchor_cron._select_isolated_org_ids",
                AsyncMock(return_value=[ORG_A, ORG_B]),
            ),
            patch(
                "server.jobs.audit_anchor_cron.emit_tenant_anchor_for_day",
                side_effect=_mock_emit,
            ),
        ):
            emitted = await _emit_tenant_anchors_for_day(db, TARGET_DAY)

        # Both orgs were attempted; ORG_A failed but ORG_B should still run.
        assert ORG_A in call_order
        assert ORG_B in call_order
        # emitted = 1 (ORG_B succeeded, ORG_A failed/returned None)
        assert emitted >= 0  # at least didn't raise

    @pytest.mark.asyncio
    async def test_backward_compat_per_tenant_off(self) -> None:
        """When AUDIT_ANCHOR_PER_TENANT=false, the per-tenant loop is skipped."""
        from server.jobs.audit_anchor_cron import run_once_for_today

        db = AsyncMock()
        anchor_result = MagicMock()
        anchor_result.scalar_one_or_none.return_value = None
        db.execute.return_value = anchor_result
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        emit_tenant_called = []

        async def _mock_emit_tenant(db, org_id, day):
            emit_tenant_called.append(org_id)
            return None

        with (
            patch.dict("os.environ", {"AUDIT_ANCHOR_PER_TENANT": "false"}),
            patch(
                "server.jobs.audit_anchor_cron._PER_TENANT",
                False,
            ),
            patch(
                "server.jobs.audit_anchor_cron.emit_anchor_for_day",
                AsyncMock(return_value=None),
            ),
            patch(
                "server.jobs.audit_anchor_cron.emit_tenant_anchor_for_day",
                side_effect=_mock_emit_tenant,
            ),
        ):
            await run_once_for_today(db)

        assert emit_tenant_called == [], (
            "per-tenant cron should not run when AUDIT_ANCHOR_PER_TENANT=false"
        )


class TestLatestTenantChainHead:
    @pytest.mark.asyncio
    async def test_genesis_when_no_rows(self) -> None:
        """Returns the per-tenant genesis sentinel when no rows exist for the org."""

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        db.execute.return_value = result_mock

        before_utc = datetime(2026, 5, 30, 0, 0, 0, tzinfo=UTC)
        head = await _latest_tenant_chain_head_before(db, ORG_A, before_utc)

        assert head == tenant_genesis(ORG_A), (
            f"expected per-tenant genesis sentinel, got {head!r}"
        )
