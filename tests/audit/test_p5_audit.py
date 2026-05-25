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
def test_export_roundtrip_json(fresh_db) -> None:
    """JSON (NDJSON) export contains every row + prev_hash; verifier round-trips."""
    import asyncio
    import json

    from server.services.audit_export import stream_json
    from server.services.audit_chain import verify_chain

    engine, SessionLocal = fresh_db
    _write_three_events(SessionLocal)

    async def _collect():
        async with SessionLocal() as s:
            chunks = []
            async for chunk in stream_json(s):
                chunks.append(chunk)
            return b"".join(chunks)
    payload = asyncio.new_event_loop().run_until_complete(_collect())

    # Parse NDJSON.
    lines = [line for line in payload.decode("utf-8").splitlines() if line.strip()]
    rows = [json.loads(line) for line in lines]
    assert len(rows) == 3, f"expected 3 rows, got {len(rows)}"
    for r in rows:
        assert "prev_hash" in r, "exported row missing prev_hash"
        assert "id" in r and "action" in r and "created_at" in r

    # Re-run the chain verifier on the exported (deserialized) rows.
    ok, bad_idx, reason = verify_chain(rows)
    assert ok, f"exported chain failed verification at {bad_idx}: {reason}"


@pytest.mark.audit
def test_export_csv(fresh_db) -> None:
    """CSV export contains the right columns including prev_hash."""
    import asyncio
    import csv
    import io

    from server.services.audit_export import CSV_COLUMNS, stream_csv

    engine, SessionLocal = fresh_db
    _write_three_events(SessionLocal)

    async def _collect():
        async with SessionLocal() as s:
            chunks = []
            async for chunk in stream_csv(s):
                chunks.append(chunk)
            return b"".join(chunks)
    payload = asyncio.new_event_loop().run_until_complete(_collect())

    reader = csv.reader(io.StringIO(payload.decode("utf-8")))
    header = next(reader)
    assert header == CSV_COLUMNS, (
        f"CSV header mismatch:\nexpected {CSV_COLUMNS}\ngot {header}"
    )
    body = list(reader)
    assert len(body) == 3, f"expected 3 data rows, got {len(body)}"
    # Every row should have a prev_hash filled.
    prev_hash_idx = CSV_COLUMNS.index("prev_hash")
    for row in body:
        assert row[prev_hash_idx], "prev_hash empty in CSV row"


@pytest.mark.audit
def test_external_verify_script_round_trip(fresh_db, tmp_path) -> None:
    """`python -m server.audit verify-chain <exported.ndjson>` exits 0 on
    a valid export and non-zero on a tampered one."""
    import asyncio
    import subprocess
    import sys

    from server.services.audit_export import stream_json
    from tests.audit.conftest import WEB_SERVER_ROOT

    engine, SessionLocal = fresh_db
    _write_three_events(SessionLocal)

    # Write the export to disk.
    out = tmp_path / "audit.ndjson"
    async def _dump():
        async with SessionLocal() as s:
            with open(out, "wb") as f:
                async for chunk in stream_json(s):
                    f.write(chunk)
    asyncio.new_event_loop().run_until_complete(_dump())

    # Verify (should pass).
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(WEB_SERVER_ROOT)}
    result = subprocess.run(
        [sys.executable, "-m", "server.audit", "verify-chain", str(out)],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, (
        f"verify exited {result.returncode}; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "OK" in result.stdout or "ok" in result.stdout.lower()

    # Tamper a row and verify again — should now fail.
    lines = out.read_text().splitlines()
    import json as _json
    row = _json.loads(lines[1])
    row["action"] = "tampered.from.disk"
    lines[1] = _json.dumps(row)
    out.write_text("\n".join(lines) + "\n")

    result = subprocess.run(
        [sys.executable, "-m", "server.audit", "verify-chain", str(out)],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode != 0, "tampered export should fail verification"


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
