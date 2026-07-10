"""Tests for audit_anchor_cron.py (#43 PR-1b2).

Covers:
- First-anchor handling: no anchors, no audit rows → emits with
  chain_head = GENESIS.
- First-anchor handling: no anchors, audit rows exist → emits with
  the latest prev_hash.
- Zero-row-day: emits anchor with same chain_head as previous day's.
- Backfill: missed N days emits N anchors with correct end-of-day
  chain heads.
- Idempotency: a second call for the same day returns None and does
  NOT create a duplicate row.
- Classification tampering detection: changing a row's
  classification after the anchor invalidates the anchor's signature
  (verifier recomputes the cls_hash and gets a different signature
  than what's stored).
- UTC discipline: anchor's signed_at is always end-of-day UTC.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).parent.parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.crypto import kms as kms_mod
from server.database.models import AuditAnchor, AuditLog
from server.jobs import audit_anchor_cron as cron
from server.services import audit_anchor as svc


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _fernet_test_key() -> str:
    import base64

    return base64.urlsafe_b64encode(b"\xaa" * 32).decode()


@pytest.fixture(autouse=True)
def _kms_env(monkeypatch):
    """Every test in this module needs a working Fernet backend."""
    monkeypatch.setenv("KMS_BACKEND", "fernet")
    monkeypatch.setenv("KMS_FERNET_KEY", _fernet_test_key())
    kms_mod.reset_backend_cache()
    yield


# ---------------------------------------------------------------------------
# First-anchor cases
# ---------------------------------------------------------------------------


def test_first_anchor_no_rows_uses_genesis(fresh_db):
    """No audit rows + no prior anchors → anchor.chain_head = GENESIS."""
    _engine, SessionLocal = fresh_db

    async def _go():
        async with SessionLocal() as db:
            row = await cron.emit_anchor_for_day(db, date(2026, 5, 27))
            await db.commit()
            return row

    row = _run(_go())
    assert row is not None
    assert row.chain_head_hash == svc.GENESIS_CHAIN_HEAD
    assert row.key_version == 1


def test_first_anchor_with_rows_uses_outgoing_chain_head(fresh_db):
    """No prior anchors but audit rows exist → anchor's chain_head_hash
    is the OUTGOING hash of the last row (compute_hash(last.prev_hash,
    last)), NOT just last.prev_hash. This matches what the verifier
    expects after replaying the chain through the last row."""
    from server.services.audit_chain import (
        compute_hash,
        serialize_for_export,
    )

    _engine, SessionLocal = fresh_db

    async def _go():
        async with SessionLocal() as db:
            db.add(
                AuditLog(
                    id="a-001",
                    action="test.event",
                    resource_type="test",
                    created_at=datetime(2026, 5, 27, 10, 0, 0),
                    prev_hash="aa" * 32,
                    classification="internal",
                )
            )
            r2 = AuditLog(
                id="a-002",
                action="test.event",
                resource_type="test",
                created_at=datetime(2026, 5, 27, 14, 0, 0),
                prev_hash="bb" * 32,
                classification="internal",
            )
            db.add(r2)
            await db.commit()
            await db.refresh(r2)

            row = await cron.emit_anchor_for_day(db, date(2026, 5, 27))
            await db.commit()
            return row, serialize_for_export(r2), r2.prev_hash

    row, r2_serialized, r2_prev = _run(_go())
    assert row is not None
    expected = compute_hash(r2_prev, r2_serialized)
    assert row.chain_head_hash == expected


# ---------------------------------------------------------------------------
# Zero-row days
# ---------------------------------------------------------------------------


def test_zero_row_day_emits_anchor_with_same_head(fresh_db):
    """A quiescent day still emits an anchor; chain_head matches the
    last non-empty day's head (same outgoing hash since no new rows
    have been added)."""
    _engine, SessionLocal = fresh_db

    async def _go():
        # Day 1 has a row.
        async with SessionLocal() as db:
            db.add(
                AuditLog(
                    id="day1-row",
                    action="x",
                    resource_type="x",
                    created_at=datetime(2026, 5, 27, 12, 0, 0),
                    prev_hash="dd" * 32,
                    classification="internal",
                )
            )
            await db.commit()
            a1 = await cron.emit_anchor_for_day(db, date(2026, 5, 27))
            await db.commit()
        # Day 2 has nothing new.
        async with SessionLocal() as db:
            a2 = await cron.emit_anchor_for_day(db, date(2026, 5, 28))
            await db.commit()
        return a1, a2

    a1, a2 = _run(_go())
    assert a1 is not None and a2 is not None
    # Both anchors sign the same chain head — that's "quiescent day".
    # The hash is the outgoing chain head of day1-row, not "dd"*32.
    assert a1.chain_head_hash == a2.chain_head_hash
    assert a1.chain_head_hash != "dd" * 32  # not the raw prev_hash


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def test_backfill_emits_one_per_missed_day(fresh_db):
    """When startup runs after 3 days of downtime, 3 anchors land
    (one per missed day, exclusive of today)."""
    _engine, SessionLocal = fresh_db

    async def _go():
        async with SessionLocal() as db:
            # Anchor for May 24 exists; today = May 28; expect
            # backfill for May 25, 26, 27 = 3 anchors.
            # Seed the May 24 anchor manually.
            await svc.ensure_active_key(db)
            await db.commit()
            db.add(
                AuditAnchor(
                    chain_head_hash=svc.GENESIS_CHAIN_HEAD,
                    signature="00" * 32,
                    signed_at=datetime(2026, 5, 25, 0, 0, 0),  # day_end of May 24
                    key_version=1,
                )
            )
            await db.commit()
            emitted = await cron.backfill_missing_anchors(
                db,
                today_utc=date(2026, 5, 28),
            )
            return emitted

    emitted = _run(_go())
    assert emitted == 3


def test_backfill_emits_nothing_when_caught_up(fresh_db):
    _engine, SessionLocal = fresh_db

    async def _go():
        async with SessionLocal() as db:
            await svc.ensure_active_key(db)
            await db.commit()
            # Anchor for yesterday exists; today = May 28; nothing
            # to do.
            db.add(
                AuditAnchor(
                    chain_head_hash=svc.GENESIS_CHAIN_HEAD,
                    signature="00" * 32,
                    signed_at=datetime(2026, 5, 28, 0, 0, 0),  # day_end of May 27
                    key_version=1,
                )
            )
            await db.commit()
            return await cron.backfill_missing_anchors(
                db,
                today_utc=date(2026, 5, 28),
            )

    emitted = _run(_go())
    assert emitted == 0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_emit_twice_for_same_day_skips_second(fresh_db):
    """Second call returns None (an anchor already exists)."""
    _engine, SessionLocal = fresh_db

    async def _go():
        async with SessionLocal() as db:
            r1 = await cron.emit_anchor_for_day(db, date(2026, 5, 27))
            await db.commit()
        async with SessionLocal() as db:
            r2 = await cron.emit_anchor_for_day(db, date(2026, 5, 27))
            await db.commit()
        return r1, r2

    r1, r2 = _run(_go())
    assert r1 is not None
    assert r2 is None  # idempotent skip


# ---------------------------------------------------------------------------
# Tampering detection at anchor-verify time
# ---------------------------------------------------------------------------


def test_classification_tampering_invalidates_anchor(fresh_db):
    """Flip a row's classification AFTER the anchor; verifier recomputes
    cls_hash and gets a different signature than what's stored."""
    _engine, SessionLocal = fresh_db

    async def _go():
        async with SessionLocal() as db:
            db.add(
                AuditLog(
                    id="t-001",
                    action="x",
                    resource_type="x",
                    created_at=datetime(2026, 5, 27, 12, 0, 0),
                    prev_hash="ee" * 32,
                    classification="confidential",
                )
            )
            await db.commit()
            anchor = await cron.emit_anchor_for_day(db, date(2026, 5, 27))
            await db.commit()
            anchor_id = anchor.id

        # Attacker flips classification confidential → public.
        async with SessionLocal() as db:
            row = await db.get(AuditLog, "t-001")
            row.classification = "public"
            await db.commit()

        # Verifier recomputes cls_hash + checks signature.
        async with SessionLocal() as db:
            from sqlalchemy import select as _select

            stmt = _select(AuditAnchor).where(AuditAnchor.id == anchor_id)
            res = await db.execute(stmt)
            stored = res.scalar_one()
            key = await svc.load_key_by_version(db, stored.key_version)

            # Recompute what the verifier would compute now (post-tamper).
            day_end = datetime(2026, 5, 28, 0, 0, 0, tzinfo=timezone.utc)
            recomputed_cls = await cron._classifications_hash_before(
                db,
                day_end,
            )
            recomputed_input = f"{stored.chain_head_hash}|{recomputed_cls}"
            recomputed_sig = svc.sign_anchor(recomputed_input, key)

        assert recomputed_sig != stored.signature, (
            "tampering with classification should invalidate the anchor"
        )

    _run(_go())


# ---------------------------------------------------------------------------
# UTC discipline
# ---------------------------------------------------------------------------


def test_signed_at_is_day_end_utc(fresh_db):
    """The anchor for day D has signed_at = 00:00 UTC of D+1."""
    _engine, SessionLocal = fresh_db

    async def _go():
        async with SessionLocal() as db:
            row = await cron.emit_anchor_for_day(db, date(2026, 5, 27))
            await db.commit()
            return row

    row = _run(_go())
    assert row is not None
    assert row.signed_at == datetime(2026, 5, 28, 0, 0, 0)
    # Naive (no tzinfo) — Postgres TIMESTAMP WITHOUT TIME ZONE.
    assert row.signed_at.tzinfo is None
