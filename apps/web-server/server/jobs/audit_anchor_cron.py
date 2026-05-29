"""Daily audit-chain anchor cron (Epic #35 #43 PR-1b2 + v1.2 #208).

What this job does
------------------

Once per day at 00:00 UTC, reads the latest ``audit_logs.prev_hash``,
combines it with a hash of the day's row classifications, signs the
combined value with the active KMS-wrapped signing key, and writes
an ``audit_anchors`` row.

v1.2 #208 adds per-tenant iteration: when ``AUDIT_ANCHOR_PER_TENANT=true``
the cron iterates over every isolated org (``isolation_mode='isolated'``)
and emits one anchor per org per day, using the org's per-tenant signing
key. The shared-chain anchor is still emitted for non-isolated tenants +
pre-cutover rows. Failure on one tenant does NOT block others.

External verifiers receive both rows + anchors in the NDJSON export
(see PR ahead). They re-compute the chain, re-compute the
classification-window hash, and verify the anchor's HMAC.

What's signed
-------------

``anchor_input = chain_head_hash + "|" + classifications_hash``

where ``classifications_hash`` is SHA-256 of the canonical encoding
of every audit row's ``(id, classification)`` pair in the window,
sorted by id. This means:

- A tampering with any classification post-write changes the
  classifications_hash, which changes the anchor_input, which makes
  the HMAC verification fail. Detection at anchor-verify time.
- The chain itself is untouched — pre-#43 audit logs keep verifying
  via ``audit_chain.verify_chain``. This is a deliberate departure
  from design decision #5; see the design doc revision note.

Cadence + idempotency
---------------------

Cron triggers at 00:00 UTC. The unique-on-UTC-date constraint on
``audit_anchors`` (Postgres expression index from PR-1a; extended to
(org_id, date) in v1.2 PR-1) makes concurrent triggers race-safe: one
wins, the other 409s. SQLite test paths use an application-side guard.

Startup backfill
----------------

On startup, the job checks the last-anchored UTC date. For every
missed day between then and yesterday (inclusive), it emits an
anchor scoped to that day's end (chain head + classifications as
they were at ``day_end(D) UTC``).

Zero-row days
-------------

A day with no new audit_logs rows still emits an anchor. The
chain_head_hash is the same as the previous day's. Operators see
consecutive anchors with identical chain_head_hash as "quiescent
day" evidence.

First anchor
------------

If no anchors exist AND no audit rows exist, emit an anchor with
chain_head_hash = ``"GENESIS"``. If no anchors exist but audit rows
do, emit with the latest row's ``prev_hash``.

Failure-safe
------------

Every code path wraps the anchor-write in try/except. A broken
KMS / DB / classification-hash failure logs WARNING but never
crashes the calling lifespan. The next day's tick retries.
Per-tenant mode: one tenant's KMS-unwrap failure does NOT block others.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.models import AuditAnchor, AuditLog, Organization, TenantAuditState
from ..services.audit_anchor import (
    GENESIS_CHAIN_HEAD,
    _SigningKey,
    ensure_active_key,
    sign_anchor,
)

logger = logging.getLogger(__name__)

# Feature flag: when true, iterate per-tenant and emit per-org anchors.
# Default off so pre-v1.2 deployments run unchanged.
_PER_TENANT = os.environ.get("AUDIT_ANCHOR_PER_TENANT", "false").lower() == "true"

# Batch commit size for the per-tenant loop (design finding #2).
# Every N tenants the cron commits + flushes; a failure within a batch
# rolls back only that batch.
_BATCH_SIZE = int(os.environ.get("AUDIT_ANCHOR_BATCH_SIZE", "100"))

# LRU key cache for unwrapped signing keys (design finding #1).
# Avoids N KMS round-trips for N tenants; re-fetches on cache miss.
# Size is deliberately modest — operators at 10k+ tenants set
# AUDIT_ANCHOR_KEY_CACHE_SIZE=10000 via the Helm chart.
_KEY_CACHE_SIZE = int(os.environ.get("AUDIT_ANCHOR_KEY_CACHE_SIZE", "1000"))


@lru_cache(maxsize=_KEY_CACHE_SIZE)
def _cached_unwrap_key(version: int, wrapped_hex: str) -> bytes:
    """LRU-cached KMS unwrap (design finding #1).

    Cache key = (version, hex(wrapped_key)) — the wrapped bytes are
    deterministic for a given version, so the unwrapped bytes are too.
    First call per version hits KMS; subsequent calls use the cache.

    Cache is in-process; evicted on pod restart (acceptable — daily
    cron typically runs from a warm pod, so the first call after
    startup pays the KMS round-trip cost).
    """
    from ..crypto.kms import get_backend

    raw = get_backend().decrypt(bytes.fromhex(wrapped_hex))
    assert len(raw) == 32, f"KMS unwrap returned {len(raw)} bytes, expected 32"
    return raw


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def emit_anchor_for_day(
    db: AsyncSession, target_day: date,
) -> AuditAnchor | None:
    """Emit a signed anchor covering rows with ``created_at < day_end(target_day)``.

    Returns the inserted AuditAnchor row, or None when an anchor for
    that UTC day already exists (idempotent).

    Wraps the entire flow in failure-safety so cron callers never
    crash on a bad anchor.
    """
    try:
        existing = await _existing_anchor_for_day(db, target_day)
        if existing is not None:
            logger.debug(
                "audit anchor: %s already has an anchor (v%d); skipping",
                target_day, existing.key_version,
            )
            return None

        signing_key = await ensure_active_key(db)
        day_end_utc = _day_end_utc(target_day)

        chain_head = await _latest_chain_head_before(db, day_end_utc)
        cls_hash = await _classifications_hash_before(db, day_end_utc)
        anchor_input = f"{chain_head}|{cls_hash}"
        signature = sign_anchor(anchor_input, signing_key)

        row = AuditAnchor(
            chain_head_hash=chain_head,
            signature=signature,
            signed_at=day_end_utc.replace(tzinfo=None),
            key_version=signing_key.version,
        )
        db.add(row)
        await db.flush()
        logger.info(
            "audit anchor: emitted for %s (chain_head=%s..., v%d)",
            target_day, chain_head[:8], signing_key.version,
        )
        return row
    except IntegrityError:
        # Concurrent trigger won the unique-on-date race.
        await db.rollback()
        logger.info(
            "audit anchor: lost race for %s; another trigger already wrote it",
            target_day,
        )
        return None
    except Exception:
        logger.warning(
            "audit anchor: emit failed for %s; will retry on next tick",
            target_day, exc_info=True,
        )
        await db.rollback()
        return None


async def backfill_missing_anchors(
    db: AsyncSession, *, today_utc: date | None = None,
) -> int:
    """Emit anchors for every UTC day between the last-anchored date
    and yesterday (inclusive). Returns the count of newly-emitted rows.

    Called on startup so a pod restart after a multi-day outage
    catches up cleanly.

    ``today_utc`` is injected for testability; default is today's
    UTC date.
    """
    today = today_utc or _utc_today()

    last = await _last_anchored_date(db)
    if last is None:
        # No anchors yet — emit one for yesterday as the bootstrap.
        # (We don't emit for "today" because the day isn't done yet.)
        target = today - timedelta(days=1)
        row = await emit_anchor_for_day(db, target)
        if row is not None:
            await db.commit()
            return 1
        return 0

    # Anchors exist; emit for each missed day [last+1 .. yesterday].
    yesterday = today - timedelta(days=1)
    if last >= yesterday:
        return 0

    emitted = 0
    cursor = last + timedelta(days=1)
    while cursor <= yesterday:
        row = await emit_anchor_for_day(db, cursor)
        if row is not None:
            emitted += 1
        cursor += timedelta(days=1)

    if emitted > 0:
        await db.commit()
    return emitted


async def run_once_for_today(db: AsyncSession) -> AuditAnchor | None:
    """Cron entry point: emit yesterday's anchor.

    Run at 00:00 UTC. "Yesterday" because the new day just started and
    every row of yesterday is now in the chain.

    v1.2 #208: when ``AUDIT_ANCHOR_PER_TENANT=true``, also iterates over
    every isolated org and emits a per-tenant anchor (one per org per day).
    Per-tenant failures are failure-safe: one tenant's KMS error doesn't
    block the rest.
    """
    yesterday = _utc_today() - timedelta(days=1)

    # 1. Shared-chain anchor (always; backward compat).
    row = await emit_anchor_for_day(db, yesterday)
    if row is not None:
        await db.commit()

    # 2. Per-tenant anchors (when feature flag is on).
    if _PER_TENANT:
        await _emit_tenant_anchors_for_day(db, yesterday)

    return row


async def emit_tenant_anchor_for_day(
    db: AsyncSession,
    org_id: str,
    target_day: date,
) -> AuditAnchor | None:
    """Emit a signed per-tenant anchor for ``org_id`` on ``target_day``.

    Idempotent: returns None if an anchor for (org_id, target_day) already
    exists. Wraps the entire flow in failure-safety so the cron loop can
    continue to the next tenant on any error.

    Uses the org's per-tenant signing key (not the shared deployment key).
    The cron acquires no per-tenant lock here — the unique-on-(org_id, day)
    DB constraint is the idempotency guard; the cron is the only writer
    of anchor rows for a given (org_id, day) pair.
    """
    try:
        # Idempotency check.
        existing = await _existing_anchor_for_org_day(db, org_id, target_day)
        if existing is not None:
            logger.debug(
                "audit anchor: org=%s %s already anchored (v%d); skipping",
                org_id, target_day, existing.key_version,
            )
            return None

        # Load the org's active signing key.
        signing_key = await _load_tenant_signing_key(db, org_id)
        if signing_key is None:
            logger.warning(
                "audit anchor: no active signing key for org=%s; "
                "skipping (key not yet issued — reconciler will issue on next pass)",
                org_id,
            )
            return None

        day_end_utc = _day_end_utc(target_day)

        chain_head = await _latest_tenant_chain_head_before(
            db, org_id, day_end_utc,
        )
        cls_hash = await _classifications_hash_before_for_org(
            db, org_id, day_end_utc,
        )
        anchor_input = f"{chain_head}|{cls_hash}"
        signature = sign_anchor(anchor_input, signing_key)

        anchor_row = AuditAnchor(
            chain_head_hash=chain_head,
            signature=signature,
            signed_at=day_end_utc.replace(tzinfo=None),
            key_version=signing_key.version,
            org_id=org_id,
        )
        db.add(anchor_row)
        await db.flush()

        # Update tenant_audit_state.last_anchor_at (health-check aid,
        # design rec #5). Best-effort: flush failure here rolls back the
        # anchor too (same tx), which is fine — the unique constraint will
        # let the next day's pass retry.
        state_stmt = select(TenantAuditState).where(
            TenantAuditState.org_id == org_id,
        )
        state = (await db.execute(state_stmt)).scalar_one_or_none()
        if state is not None:
            state.last_anchor_at = day_end_utc.replace(tzinfo=None)

        logger.info(
            "audit anchor: per-tenant emitted for org=%s %s "
            "(chain_head=%s..., v%d)",
            org_id, target_day, chain_head[:8], signing_key.version,
        )
        return anchor_row

    except IntegrityError:
        # Concurrent trigger or duplicate from a re-run — treat as success.
        await db.rollback()
        logger.info(
            "audit anchor: lost race for org=%s %s; already written",
            org_id, target_day,
        )
        return None
    except Exception:
        logger.warning(
            "audit anchor: per-tenant emit failed for org=%s %s; "
            "will retry on next tick",
            org_id, target_day, exc_info=True,
        )
        await db.rollback()
        return None


async def _emit_tenant_anchors_for_day(
    db: AsyncSession, target_day: date,
) -> int:
    """Iterate over isolated orgs and emit one anchor per org.

    Processes orgs in id order (stable, deterministic). Commits every
    _BATCH_SIZE orgs (design finding #2). One tenant's failure doesn't
    block others (design failure-safe contract).

    Returns the count of successfully emitted anchors.
    """
    iso_org_ids = await _select_isolated_org_ids(db)
    emitted = 0
    batch_count = 0

    for org_id in iso_org_ids:
        try:
            row = await emit_tenant_anchor_for_day(db, org_id, target_day)
        except Exception:
            # Failure-safe: one tenant's exception must not block others.
            # emit_tenant_anchor_for_day already catches most exceptions;
            # this outer guard handles any unexpected raises.
            logger.warning(
                "audit anchor: unexpected error in per-tenant loop for org=%s; "
                "continuing to next tenant",
                org_id, exc_info=True,
            )
            row = None
        if row is not None:
            emitted += 1
        batch_count += 1
        if batch_count >= _BATCH_SIZE:
            await db.commit()
            batch_count = 0

    if batch_count > 0:
        await db.commit()

    logger.info(
        "audit anchor: per-tenant pass complete — %d emitted / %d orgs",
        emitted, len(iso_org_ids),
    )
    return emitted


async def _select_isolated_org_ids(db: AsyncSession) -> list[str]:
    """Return org IDs for tenants with isolation_mode='isolated' that have
    an active per-tenant signing key (i.e. tenant_audit_state row exists).

    Joining on tenant_audit_state ensures we only iterate orgs that are
    fully provisioned for per-tenant chains (key issued + genesis written).
    Orgs whose key issuance failed fall back to the shared chain silently.
    """
    from ..database.models import TenantState

    stmt = (
        select(Organization.id)
        .join(TenantState, TenantState.org_id == Organization.id)
        .join(
            TenantAuditState,
            TenantAuditState.org_id == Organization.id,
        )
        .where(TenantState.isolation_mode == "isolated")
        .where(Organization.deleted_at.is_(None))
        .where(TenantAuditState.lifecycle == "active")
        .order_by(Organization.id.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _load_tenant_signing_key(
    db: AsyncSession, org_id: str,
) -> _SigningKey | None:
    """Load + unwrap the active signing key for ``org_id``.

    Uses the LRU cache to avoid a KMS round-trip on every cron tick.
    Returns None when no active key exists (not yet provisioned).
    """
    from ..database.models import AuditSigningKey

    stmt = (
        select(AuditSigningKey)
        .where(AuditSigningKey.org_id == org_id)
        .where(AuditSigningKey.retired_at.is_(None))
        .order_by(AuditSigningKey.version.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    key_row = result.scalar_one_or_none()
    if key_row is None:
        return None

    raw = _cached_unwrap_key(key_row.version, key_row.wrapped_key.hex())
    return _SigningKey(raw=raw, version=key_row.version)


async def _existing_anchor_for_org_day(
    db: AsyncSession, org_id: str, d: date,
) -> AuditAnchor | None:
    """Look up an anchor for (org_id, day d). Used by per-tenant idempotency."""
    day_start = datetime.combine(d, time(0, 0, 0))
    day_end = datetime.combine(d + timedelta(days=1), time(0, 0, 0))
    stmt = (
        select(AuditAnchor)
        .where(AuditAnchor.org_id == org_id)
        .where(AuditAnchor.signed_at > day_start)
        .where(AuditAnchor.signed_at <= day_end)
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _latest_tenant_chain_head_before(
    db: AsyncSession, org_id: str, before_utc: datetime,
) -> str:
    """Per-tenant equivalent of ``_latest_chain_head_before`` scoped to org.

    Returns the per-tenant genesis sentinel when no rows exist for the org
    before ``before_utc``.
    """
    from ..services.audit_chain import (
        compute_tenant_hash,
        serialize_for_export,
        tenant_genesis,
    )

    before_naive = before_utc.replace(tzinfo=None)
    stmt = (
        select(AuditLog)
        .where(AuditLog.org_id == org_id)
        .where(AuditLog.created_at < before_naive)
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        return tenant_genesis(org_id)

    # Outgoing hash uses compute_tenant_hash for domain separation.
    return compute_tenant_hash(
        row.prev_hash or tenant_genesis(org_id),
        serialize_for_export(row),
        org_id,
    )


async def _classifications_hash_before_for_org(
    db: AsyncSession, org_id: str, before_utc: datetime,
) -> str:
    """Per-tenant classifications hash, scoped to ``org_id``."""
    before_naive = before_utc.replace(tzinfo=None)
    stmt = (
        select(AuditLog.id, AuditLog.classification)
        .where(AuditLog.org_id == org_id)
        .where(AuditLog.created_at < before_naive)
        .order_by(AuditLog.id.asc())
    )
    result = await db.execute(stmt)
    h = hashlib.sha256()
    any_rows = False
    for row in result:
        any_rows = True
        h.update(row[0].encode("utf-8"))
        h.update(b"\x1f")
        h.update((row[1] or "").encode("utf-8"))
        h.update(b"\x1e")
    if not any_rows:
        return hashlib.sha256(b"").hexdigest()
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _utc_today() -> date:
    """Today as a UTC date. Centralised so the timezone discipline
    can be audited in one place."""
    return datetime.now(timezone.utc).date()


def _day_end_utc(d: date) -> datetime:
    """The exclusive upper bound for rows in this anchor's window:
    00:00 UTC of the FOLLOWING day."""
    return datetime.combine(d + timedelta(days=1), time(0, 0, 0)).replace(
        tzinfo=timezone.utc,
    )


async def _last_anchored_date(db: AsyncSession) -> date | None:
    """Return the UTC date of the most recent anchor, or None."""
    stmt = select(AuditAnchor.signed_at).order_by(
        AuditAnchor.signed_at.desc(),
    ).limit(1)
    result = await db.execute(stmt)
    last = result.scalar_one_or_none()
    if last is None:
        return None
    # signed_at is the day_end_utc we wrote (= start of day d+1). The
    # actual covered date is (signed_at - 1 second).date(). Robust
    # for any same-day boundary representation.
    return (last - timedelta(seconds=1)).date()


async def _existing_anchor_for_day(
    db: AsyncSession, d: date,
) -> AuditAnchor | None:
    """Look up an anchor whose signed_at falls on day d (UTC)."""
    day_start = datetime.combine(d, time(0, 0, 0))
    day_end = datetime.combine(d + timedelta(days=1), time(0, 0, 0))
    # signed_at is naive-UTC; SQLAlchemy compares cleanly.
    stmt = select(AuditAnchor).where(
        AuditAnchor.signed_at > day_start,
        AuditAnchor.signed_at <= day_end,
    ).limit(1)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _latest_chain_head_before(
    db: AsyncSession, before_utc: datetime,
) -> str:
    """The chain head AFTER the last audit_logs row with
    created_at < before_utc.

    "Chain head" matches ``audit_chain.verify_chain``'s semantics:
    it's the value that WOULD be stored as the next inserted row's
    ``prev_hash`` — i.e. ``compute_hash(last_row.prev_hash, last_row)``,
    NOT just ``last_row.prev_hash`` (which would exclude the last
    row's contribution from the signed window).

    Returns ``GENESIS_CHAIN_HEAD`` when no such row exists.
    """
    from ..services.audit_chain import compute_hash, serialize_for_export

    before_naive = before_utc.replace(tzinfo=None)
    stmt = select(AuditLog).where(
        AuditLog.created_at < before_naive,
    ).order_by(AuditLog.created_at.desc()).limit(1)
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        return GENESIS_CHAIN_HEAD
    # Outgoing hash of the last row = next-row's expected prev_hash.
    return compute_hash(
        row.prev_hash or GENESIS_CHAIN_HEAD, serialize_for_export(row),
    )


async def _classifications_hash_before(
    db: AsyncSession, before_utc: datetime,
) -> str:
    """SHA-256 hex of the canonical (id, classification) list, sorted
    by id, for every audit_logs row with created_at < before_utc.

    Empty window → SHA-256 of empty bytes (deterministic sentinel).
    """
    before_naive = before_utc.replace(tzinfo=None)
    stmt = select(AuditLog.id, AuditLog.classification).where(
        AuditLog.created_at < before_naive,
    ).order_by(AuditLog.id.asc())
    result = await db.execute(stmt)
    h = hashlib.sha256()
    any_rows = False
    for row in result:
        any_rows = True
        # 0x1f separator between id and classification; 0x1e between
        # pairs. Both characters never appear in either field.
        h.update(row[0].encode("utf-8"))
        h.update(b"\x1f")
        h.update((row[1] or "").encode("utf-8"))
        h.update(b"\x1e")
    if not any_rows:
        # Sentinel for empty window so the verifier knows what to
        # expect.
        return hashlib.sha256(b"").hexdigest()
    return h.hexdigest()


def _sign_anchor_input(chain_head: str, cls_hash: str, key: _SigningKey) -> str:
    """Compose + sign the anchor input. Exposed for test reuse."""
    return sign_anchor(f"{chain_head}|{cls_hash}", key)


# ---------------------------------------------------------------------------
# CLI entry point — for Kubernetes CronJob / manual operator runs
# ---------------------------------------------------------------------------


def main() -> None:
    """`python -m server.jobs.audit_anchor_cron` runs once."""
    from ..database.engine import async_session_factory

    async def _go():
        async with async_session_factory() as db:
            row = await run_once_for_today(db)
            if row is None:
                logger.info("audit anchor: nothing to emit (already anchored)")
            else:
                logger.info(
                    "audit anchor: emitted v%d for %s",
                    row.key_version, row.signed_at,
                )

    asyncio.run(_go())


if __name__ == "__main__":
    main()
