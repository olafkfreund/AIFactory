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


@pytest.mark.audit
@pytest.mark.skip(reason="P5.2 pending: hash chain on write")
def test_hash_chain_links_rows(fresh_db) -> None:
    """Each row's prev_hash equals the previous row's hash; first row's prev_hash is the genesis sentinel."""
    pytest.fail("P5.2 not landed")


@pytest.mark.audit
@pytest.mark.skip(reason="P5.4 pending: chain verifier")
def test_tampered_row_breaks_chain(fresh_db) -> None:
    """Mutating any row's content makes verify_chain return False at that row."""
    pytest.fail("P5.4 not landed")


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
