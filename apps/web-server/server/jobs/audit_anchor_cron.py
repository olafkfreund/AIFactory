"""Daily audit-chain anchor cron (Epic #35 #43 PR-1b2).

What this job does
------------------

Once per day at 00:00 UTC, reads the latest ``audit_logs.prev_hash``,
combines it with a hash of the day's row classifications, signs the
combined value with the active KMS-wrapped signing key, and writes
an ``audit_anchors`` row.

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
``audit_anchors`` (Postgres expression index from PR-1a) makes
concurrent triggers race-safe: one wins, the other 409s. SQLite
test paths use an application-side guard.

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
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.models import AuditAnchor, AuditLog
from ..services.audit_anchor import (
    GENESIS_CHAIN_HEAD,
    _SigningKey,
    ensure_active_key,
    sign_anchor,
)

logger = logging.getLogger(__name__)


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
    """
    yesterday = _utc_today() - timedelta(days=1)
    row = await emit_anchor_for_day(db, yesterday)
    if row is not None:
        await db.commit()
    return row


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
