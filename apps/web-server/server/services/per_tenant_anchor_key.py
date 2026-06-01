"""Per-tenant audit-anchor key issuance (Epic #35 v1.2 #208).

Called from the tenant reconciler when an org transitions to
``isolation_mode='isolated'`` and ``AUDIT_ANCHOR_PER_TENANT=true``.

What this module does
---------------------

Generates a fresh 32-byte HMAC-SHA256 key for the tenant, wraps it via
the existing KMS backend, INSERTs a row into ``audit_signing_keys`` with
``org_id`` set, writes the wrapped blob to the tenant's Vault path for
auditor handover, and upserts ``tenant_audit_state`` to record the chain
genesis.

Why separated from audit_anchor.py
-----------------------------------

The issuance flow requires a Vault client (not needed by the signing/
verification helpers). Keeping per-tenant key issuance here avoids
adding Vault coupling to audit_anchor.py and keeps each module's
responsibility clear.

Failure-safe contract
----------------------

Every external call (KMS, DB, Vault) is wrapped individually. If Vault
write fails the DB row is still the authoritative copy and the cron can
sign anchors using it. The reconciler retries the Vault write on the
next tick (the ``tenant_audit_state`` row is NOT upserted until all
three core steps succeed — preventing the chain from starting without a
key).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto.kms import get_backend
from ..database.models import AuditSigningKey, TenantAuditState
from ..services.audit_anchor import generate_new_key

logger = logging.getLogger(__name__)

# Feature flag — checked at call-site; callers should guard before calling.
_PER_TENANT_ENABLED = os.environ.get("AUDIT_ANCHOR_PER_TENANT", "false").lower() == "true"

# Vault path pattern. Flat (not nested) per design open-question #1 resolution.
_VAULT_KEY_PATH_TMPL = "aifactory/orgs/{org_uuid}/anchor-key-wrapped"
_VAULT_META_PATH_TMPL = "aifactory/orgs/{org_uuid}/anchor-key-metadata"


def per_tenant_enabled() -> bool:
    """Runtime feature-flag check. Import-time evaluation deferred so
    tests can override the env var without import-order constraints."""
    return os.environ.get("AUDIT_ANCHOR_PER_TENANT", "false").lower() == "true"


async def issue_tenant_anchor_key(
    db: AsyncSession,
    vault_client: Any,
    org_id: str,
) -> bool:
    """Provision a per-tenant audit-anchor signing key.

    Steps (design PR-1):
      1. Check idempotency — bail if an active key already exists.
      2. Generate 32 raw bytes.
      3. KMS-wrap via the existing backend.
      4. INSERT audit_signing_keys row with org_id.
      5. Write wrapped blob to Vault path (best-effort; failure logged).
      6. Upsert tenant_audit_state with genesis sentinel.

    Returns True when a new key was issued, False when the key already
    existed (idempotent), or raises on unrecoverable DB failures.
    """
    # Step 1: idempotency — if an active key already exists for this org,
    # treat the call as a no-op (reconciler may retry; don't create duplicates).
    existing = await _active_key_for_org(db, org_id)
    if existing is not None:
        logger.debug(
            "per-tenant anchor key already exists for org=%s (v%d); skipping issuance",
            org_id, existing.version,
        )
        return False

    # Step 2 + 3: generate + wrap.
    raw_key = generate_new_key()
    kms = get_backend()
    wrapped = kms.encrypt(raw_key)

    # Step 4: persist to audit_signing_keys.
    key_row = AuditSigningKey(wrapped_key=wrapped, org_id=org_id)
    db.add(key_row)
    await db.flush()
    # key_row.version is now set by the DB autoincrement.

    logger.info(
        "per-tenant anchor key issued: org=%s version=%d",
        org_id, key_row.version,
    )

    # Step 5: mirror to Vault (best-effort — DB row is authoritative).
    # Failure here does NOT abort the issuance; the reconciler retries
    # the Vault write on next tick via the Vault-only retry path.
    if vault_client is not None:
        try:
            _write_vault_key(vault_client, org_id, wrapped, key_row.version)
        except Exception:
            logger.warning(
                "per-tenant anchor key: Vault write failed for org=%s; "
                "DB row is the authoritative copy; reconciler will retry",
                org_id, exc_info=True,
            )

    # Step 6: upsert tenant_audit_state.  All three core steps (generate,
    # wrap, persist) succeeded — now mark the chain as started.
    genesis_sentinel = f"GENESIS-T-{org_id}"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await _upsert_tenant_audit_state(db, org_id, genesis_sentinel, now)

    return True


async def _active_key_for_org(
    db: AsyncSession, org_id: str,
) -> AuditSigningKey | None:
    """Return the active (retired_at IS NULL) signing key for an org, or None."""
    stmt = (
        select(AuditSigningKey)
        .where(AuditSigningKey.org_id == org_id)
        .where(AuditSigningKey.retired_at.is_(None))
        .order_by(AuditSigningKey.version.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


def _write_vault_key(
    vault_client: Any,
    org_id: str,
    wrapped: bytes,
    version: int,
) -> None:
    """Write the wrapped key blob + metadata to the tenant's Vault path.

    The reconciler's AppRole has create+update capabilities on
    ``aifactory/data/orgs/+/anchor-key-*`` (design rec #3 + PR-3 chart).
    It does NOT have read capability (write-only preserves the trust model).

    ``vault_client`` is the sync VaultClient from ``server/services/vault_client.py``.
    """
    import json

    key_path = _VAULT_KEY_PATH_TMPL.format(org_uuid=org_id)
    meta_path = _VAULT_META_PATH_TMPL.format(org_uuid=org_id)

    # Write the raw wrapped blob (bytes → base64 by the vault client's write
    # method, which handles encoding per the backend's KV v2 API).
    vault_client.kv_put(key_path, {"value": wrapped.hex()})

    # Metadata for the auditor handover runbook.
    kms = get_backend()
    kms_key_id = getattr(kms, "key_id", "fernet:default")
    meta = {
        "version": version,
        "org_id": org_id,
        "kms_key_id": kms_key_id,
    }
    vault_client.kv_put(meta_path, {"value": json.dumps(meta)})


async def _upsert_tenant_audit_state(
    db: AsyncSession,
    org_id: str,
    genesis_hash: str,
    now: datetime,
) -> TenantAuditState:
    """Create or (no-op) return the tenant_audit_state row for this org.

    If a row already exists (idempotent re-run), leave it unchanged —
    the chain may have progressed and we must NOT reset the head.
    """
    stmt = select(TenantAuditState).where(TenantAuditState.org_id == org_id)
    result = await db.execute(stmt)
    state = result.scalar_one_or_none()
    if state is not None:
        return state

    state = TenantAuditState(
        org_id=org_id,
        chain_started_at=now,
        current_head_hash=genesis_hash,
        lifecycle="active",
    )
    db.add(state)
    await db.flush()
    return state


async def ensure_vault_key_written(
    db: AsyncSession,
    vault_client: Any,
    org_id: str,
) -> None:
    """Retry-only path: write the Vault copy of an already-issued key.

    Called by the reconciler when a prior issuance succeeded in the DB
    but failed the Vault write. Does NOT re-issue the key.
    """
    key_row = await _active_key_for_org(db, org_id)
    if key_row is None:
        logger.warning(
            "ensure_vault_key_written: no active key for org=%s; "
            "nothing to mirror", org_id,
        )
        return

    try:
        _write_vault_key(vault_client, org_id, key_row.wrapped_key, key_row.version)
        logger.info(
            "per-tenant anchor key: Vault mirror written (retry) for org=%s",
            org_id,
        )
    except Exception:
        logger.warning(
            "per-tenant anchor key: Vault write retry failed for org=%s",
            org_id, exc_info=True,
        )
