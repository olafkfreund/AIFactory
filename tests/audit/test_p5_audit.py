"""P5 — Audit hardening acceptance tests.

Seven tests map to the five acceptance bullets in Epic #26 issue #32:

  1. test_hash_chain_links_rows                — AC: chain on write
  2. test_tampered_row_breaks_chain             — AC: tampered detected
  3. test_export_roundtrip_json                 — AC: JSON export round-trip
  4. test_export_csv                            — AC: CSV export
  5. test_external_verify_script_round_trip     — AC: external verify
  6. test_erasure_deletes_pii_but_chain_still_verifies  — AC: GDPR
  7. test_retention_deletes_expired             — AC: retention job
"""

from __future__ import annotations

import pytest


def _write_three_events(SessionLocal):
    """Helper: write 3 audit events via log_audit_event; return their ids."""
    import asyncio
    from server.services.audit_service import log_audit_event

    ids: list[str] = []
    async def _go():
        async with SessionLocal() as session:
            for i in range(3):
                await log_audit_event(
                    db=session,
                    action=f"test.action.{i}",
                    resource_type="test",
                    resource_id=f"r{i}",
                    user_id=None,
                    org_id=None,
                    details={"i": i},
                )
            await session.commit()
            # Fetch the inserted rows ordered.
            from sqlalchemy import select
            from server.database.models import AuditLog
            result = await session.execute(
                select(AuditLog).order_by(AuditLog.created_at.asc())
            )
            for row in result.scalars():
                ids.append(row.id)
    asyncio.new_event_loop().run_until_complete(_go())
    return ids


@pytest.mark.audit
def test_hash_chain_links_rows(fresh_db) -> None:
    """First row's prev_hash = GENESIS; each subsequent row's prev_hash =
    compute_hash(previous row). The full chain verifies via verify_chain."""
    import asyncio
    from sqlalchemy import select

    from server.database.models import AuditLog
    from server.services.audit_chain import (
        GENESIS, compute_hash, row_as_mapping, verify_chain,
    )

    engine, SessionLocal = fresh_db
    _write_three_events(SessionLocal)

    async def _fetch():
        async with SessionLocal() as s:
            result = await s.execute(
                select(AuditLog).order_by(AuditLog.created_at.asc())
            )
            return [row_as_mapping(r) for r in result.scalars()]
    rows = asyncio.new_event_loop().run_until_complete(_fetch())

    # First row's prev_hash is genesis.
    assert rows[0]["prev_hash"] == GENESIS, (
        f"first row's prev_hash must be GENESIS; got {rows[0]['prev_hash']!r}"
    )
    # Each subsequent row's prev_hash chains to the previous row.
    for i in range(1, len(rows)):
        expected = compute_hash(rows[i - 1]["prev_hash"], rows[i - 1])
        assert rows[i]["prev_hash"] == expected, (
            f"row {i} prev_hash mismatch: stored={rows[i]['prev_hash']!r} "
            f"expected={expected!r}"
        )

    # End-to-end verification.
    ok, bad_idx, reason = verify_chain(rows)
    assert ok, f"chain verification failed at row {bad_idx}: {reason}"


@pytest.mark.audit
def test_tampered_row_breaks_chain(fresh_db) -> None:
    """Mutating any row's protected content (action, details_json, etc.)
    makes verify_chain return False at the row AFTER the mutation."""
    import asyncio
    from sqlalchemy import select

    from server.database.models import AuditLog
    from server.services.audit_chain import row_as_mapping, verify_chain

    engine, SessionLocal = fresh_db
    _write_three_events(SessionLocal)

    async def _fetch_and_tamper():
        async with SessionLocal() as s:
            result = await s.execute(
                select(AuditLog).order_by(AuditLog.created_at.asc())
            )
            audit_rows = list(result.scalars())
            # Tamper the middle row's action.
            audit_rows[1].action = "tampered.action"
            await s.commit()
            # Re-fetch.
            result2 = await s.execute(
                select(AuditLog).order_by(AuditLog.created_at.asc())
            )
            return [row_as_mapping(r) for r in result2.scalars()]

    rows = asyncio.new_event_loop().run_until_complete(_fetch_and_tamper())

    ok, bad_idx, reason = verify_chain(rows)
    assert not ok, "tampered row should fail verification"
    # The chain breaks at row 2 — the row AFTER the tampered row,
    # because row 2's stored prev_hash was computed against the
    # untampered content of row 1.
    assert bad_idx == 2, (
        f"expected mismatch at row 2 (after tampered row 1); got {bad_idx} — {reason}"
    )


@pytest.mark.audit
@pytest.mark.skip(reason="P5.3 pending: export endpoint")
def test_export_roundtrip_json(fresh_db) -> None:
    """JSON export contains every row + prev_hash; verifier round-trips."""
    pytest.fail("P5.3 not landed")


@pytest.mark.audit
@pytest.mark.skip(reason="P5.3 pending: export endpoint")
def test_export_csv(fresh_db) -> None:
    """CSV export contains the right columns including prev_hash."""
    pytest.fail("P5.3 not landed")


@pytest.mark.audit
@pytest.mark.skip(reason="P5.4 pending: external verify CLI")
def test_external_verify_script_round_trip(fresh_db, tmp_path) -> None:
    """python -m server.audit verify-chain <exported.json> exits 0."""
    pytest.fail("P5.4 not landed")


@pytest.mark.audit
@pytest.mark.skip(reason="P5.5 pending: GDPR erasure")
def test_erasure_deletes_pii_but_chain_still_verifies(fresh_db) -> None:
    """After GDPR erasure: users.email/name are NULL, audit chain still verifies,
    audit user_id is sha256(original_user_id) — non-reversible."""
    pytest.fail("P5.5 not landed")


@pytest.mark.audit
@pytest.mark.skip(reason="P5.6 pending: retention job")
def test_retention_deletes_expired(fresh_db) -> None:
    """Rows past retention_until are deleted by the retention job."""
    pytest.fail("P5.6 not landed")
