"""Tests for audit_export.py anchor interleave + verifier (#43 PR-1b3).

Covers:
- Anchor placement: each anchor lands AFTER the last row whose
  created_at < anchor.signed_at.
- Verifier round-trip: a stream from cron-produced anchors verifies
  cleanly.
- Verifier rejects when chain is tampered.
- Verifier rejects when an anchor signature is tampered.
- Verifier rejects when classification on a row is tampered post-anchor.
- Pending window: rows after the last anchor are accepted (not failures).
- Trailing rows with no anchor at all → all rows verified, no anchors.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).parent.parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.crypto import kms as kms_mod
from server.database.models import AuditLog
from server.jobs import audit_anchor_cron as cron
from server.services import audit_anchor as svc
from server.services import audit_export


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
    monkeypatch.setenv("KMS_BACKEND", "fernet")
    monkeypatch.setenv("KMS_FERNET_KEY", _fernet_test_key())
    kms_mod.reset_backend_cache()
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_chain(SessionLocal) -> None:
    """Build a 3-row chain spanning two days with proper prev_hash linkage."""
    from server.services.audit_chain import GENESIS, compute_hash, serialize_for_export
    async with SessionLocal() as db:
        # Pre-compute prev_hash for each row so the chain verifies.
        rows_data = [
            ("r-001", datetime(2026, 5, 27, 10, 0, 0), "internal"),
            ("r-002", datetime(2026, 5, 27, 14, 0, 0), "confidential"),
            ("r-003", datetime(2026, 5, 28, 9, 0, 0), "internal"),
        ]
        prev = GENESIS
        for rid, ts, cls in rows_data:
            row = AuditLog(
                id=rid, action="test.event", resource_type="test",
                created_at=ts, prev_hash=prev, classification=cls,
            )
            db.add(row)
            await db.flush()
            # Re-fetch the row's content so compute_hash sees the persisted
            # values (in particular created_at after the server default).
            await db.refresh(row)
            prev = compute_hash(prev, serialize_for_export(row))
        await db.commit()


async def _collect(gen) -> bytes:
    out = b""
    async for chunk in gen:
        out += chunk
    return out


async def _load_signing_keys(SessionLocal) -> dict[int, bytes]:
    """Verifier needs key_version → raw bytes."""
    from server.database.models import AuditSigningKey
    from sqlalchemy import select
    async with SessionLocal() as db:
        rows = (await db.execute(select(AuditSigningKey))).scalars().all()
        return {
            r.version: (await svc.load_key_by_version(db, r.version)).raw
            for r in rows
        }


# ---------------------------------------------------------------------------
# Interleave ordering
# ---------------------------------------------------------------------------


def test_anchor_lands_after_covered_rows(fresh_db):
    """Day-1 rows then day-1's anchor then day-2 rows then day-2's anchor."""
    _engine, SessionLocal = fresh_db

    async def _go():
        await _seed_chain(SessionLocal)
        async with SessionLocal() as db:
            await cron.emit_anchor_for_day(db, date(2026, 5, 27))
            await cron.emit_anchor_for_day(db, date(2026, 5, 28))
            await db.commit()
        async with SessionLocal() as db:
            return await _collect(audit_export.stream_json_with_anchors(db))

    raw = _run(_go())
    lines = [json.loads(line) for line in raw.decode().splitlines() if line]
    kinds = [obj["_kind"] for obj in lines]
    # Expected: row r-001, row r-002, anchor (May 27 day-end), row r-003,
    # anchor (May 28 day-end).
    assert kinds == ["row", "row", "anchor", "row", "anchor"]
    # r-001/r-002 precede the May 27 anchor.
    assert lines[0]["id"] == "r-001"
    assert lines[1]["id"] == "r-002"
    assert lines[3]["id"] == "r-003"


# ---------------------------------------------------------------------------
# Verifier — happy path
# ---------------------------------------------------------------------------


def test_verifier_accepts_clean_export(fresh_db):
    _engine, SessionLocal = fresh_db

    async def _go():
        await _seed_chain(SessionLocal)
        async with SessionLocal() as db:
            await cron.emit_anchor_for_day(db, date(2026, 5, 27))
            await cron.emit_anchor_for_day(db, date(2026, 5, 28))
            await db.commit()
        async with SessionLocal() as db:
            raw = await _collect(audit_export.stream_json_with_anchors(db))
        keys = await _load_signing_keys(SessionLocal)
        return raw, keys

    raw, keys = _run(_go())
    result = audit_export.verify_anchored_export(raw, keys)
    assert result.ok, f"unexpected failures: {result.failures}"
    assert result.rows_verified == 3
    assert result.anchors_verified == 2


# ---------------------------------------------------------------------------
# Verifier — tampering detection
# ---------------------------------------------------------------------------


def test_verifier_rejects_tampered_row_content(fresh_db):
    """Flip the action of a row in the wire stream; the chain breaks."""
    _engine, SessionLocal = fresh_db

    async def _go():
        await _seed_chain(SessionLocal)
        async with SessionLocal() as db:
            await cron.emit_anchor_for_day(db, date(2026, 5, 27))
            await db.commit()
        async with SessionLocal() as db:
            raw = await _collect(audit_export.stream_json_with_anchors(db))
        keys = await _load_signing_keys(SessionLocal)
        return raw, keys

    raw, keys = _run(_go())
    lines = raw.decode().splitlines()
    # Tamper with row r-001's action.
    tampered = []
    for line in lines:
        obj = json.loads(line)
        if obj.get("id") == "r-001":
            obj["action"] = "tampered.event"
        tampered.append(json.dumps(obj))
    tampered_raw = ("\n".join(tampered) + "\n").encode("utf-8")

    result = audit_export.verify_anchored_export(tampered_raw, keys)
    assert not result.ok
    # The chain detects the tamper at the anchor verification (chain
    # head mismatch).
    assert any("anchor" in reason or "chain" in reason
               for _, reason in result.failures), result.failures


def test_verifier_rejects_tampered_anchor_signature(fresh_db):
    _engine, SessionLocal = fresh_db

    async def _go():
        await _seed_chain(SessionLocal)
        async with SessionLocal() as db:
            await cron.emit_anchor_for_day(db, date(2026, 5, 27))
            await db.commit()
        async with SessionLocal() as db:
            raw = await _collect(audit_export.stream_json_with_anchors(db))
        keys = await _load_signing_keys(SessionLocal)
        return raw, keys

    raw, keys = _run(_go())
    lines = raw.decode().splitlines()
    tampered = []
    for line in lines:
        obj = json.loads(line)
        if obj.get("_kind") == "anchor":
            # Flip the first hex char of the signature.
            sig = obj["signature"]
            obj["signature"] = ("a" if sig[0] != "a" else "b") + sig[1:]
        tampered.append(json.dumps(obj))
    tampered_raw = ("\n".join(tampered) + "\n").encode("utf-8")

    result = audit_export.verify_anchored_export(tampered_raw, keys)
    assert not result.ok
    assert any("signature mismatch" in reason
               for _, reason in result.failures), result.failures


def test_verifier_rejects_classification_tamper_in_wire(fresh_db):
    """Flip classification in the wire stream → classification hash
    changes → anchor signature mismatch."""
    _engine, SessionLocal = fresh_db

    async def _go():
        await _seed_chain(SessionLocal)
        async with SessionLocal() as db:
            await cron.emit_anchor_for_day(db, date(2026, 5, 27))
            await db.commit()
        async with SessionLocal() as db:
            raw = await _collect(audit_export.stream_json_with_anchors(db))
        keys = await _load_signing_keys(SessionLocal)
        return raw, keys

    raw, keys = _run(_go())
    lines = raw.decode().splitlines()
    tampered = []
    for line in lines:
        obj = json.loads(line)
        if obj.get("id") == "r-002":
            # Flip confidential → public.
            obj["classification"] = "public"
        tampered.append(json.dumps(obj))
    tampered_raw = ("\n".join(tampered) + "\n").encode("utf-8")

    result = audit_export.verify_anchored_export(tampered_raw, keys)
    assert not result.ok
    # Either the chain detects it (if the row's prev_hash mismatches)
    # or the anchor signature mismatch fires. Either is a real failure.
    assert result.failures


# ---------------------------------------------------------------------------
# Pending window — rows after last anchor are legitimate
# ---------------------------------------------------------------------------


def test_pending_window_accepted(fresh_db):
    """Day 1 + anchor + Day 2 rows (no day-2 anchor yet) → verify
    succeeds, pending_window_rows > 0 informational."""
    _engine, SessionLocal = fresh_db

    async def _go():
        await _seed_chain(SessionLocal)
        async with SessionLocal() as db:
            # Only day-1 anchor; day-2 rows are pending.
            await cron.emit_anchor_for_day(db, date(2026, 5, 27))
            await db.commit()
        async with SessionLocal() as db:
            raw = await _collect(audit_export.stream_json_with_anchors(db))
        keys = await _load_signing_keys(SessionLocal)
        return raw, keys

    raw, keys = _run(_go())
    result = audit_export.verify_anchored_export(raw, keys)
    assert result.ok, f"unexpected failures: {result.failures}"
    assert result.rows_verified == 3
    assert result.anchors_verified == 1
    assert result.pending_window_rows >= 1


# ---------------------------------------------------------------------------
# No anchors at all — rows verify, just no anchors
# ---------------------------------------------------------------------------


def test_export_with_no_anchors(fresh_db):
    _engine, SessionLocal = fresh_db

    async def _go():
        await _seed_chain(SessionLocal)
        async with SessionLocal() as db:
            raw = await _collect(audit_export.stream_json_with_anchors(db))
        keys = await _load_signing_keys(SessionLocal)
        return raw, keys

    raw, keys = _run(_go())
    result = audit_export.verify_anchored_export(raw, keys)
    # No keys yet — but verifier should still be ok because there are
    # no anchors to check.
    assert result.anchors_verified == 0
    assert result.rows_verified == 3
    assert result.ok
