"""Hash chain for the audit log (Epic #26 P5.2 / P5.4).

The chain is a per-row SHA-256 of the previous row's canonical
content. The first row in the chain has ``prev_hash = GENESIS``.

Threat model:
  PROTECTS against: insertion / deletion / mutation of audit log
    rows by an attacker who has write access to the DB but cannot
    re-compute the chain (e.g., a compromised DB read-replica
    replayed forward).
  DOES NOT PROTECT against: an attacker who can re-compute the entire
    chain from any point forward (which any DB admin can do).
    Defense for that scenario = the daily signed audit-chain anchor
    (Epic #35 #43, shipped). The anchor signs the chain head with an
    HMAC key the DB admin doesn't have, so a rewritten chain produces
    a different head + anchor mismatch on verify. See
    ``apps/web-server/server/services/audit_anchor.py`` for the signer
    and ``docs/docs/concepts/audit-anchor.md`` for the operator-facing
    explanation.
  STILL DOES NOT PROTECT against: an attacker who has BOTH DB write
    access AND the unwrapped HMAC key. v1.2 closes that via external
    publication (S3 Object Lock / RFC 3161 TSA / Sigstore) — see the
    "What's not yet supported" section of the audit-anchor concept doc.

Canonical encoding (the bytes we SHA-256):
  GENESIS for first row, else previous row's hash || \\x1f ||
  current row's content as ``id|action|user_id|org_id|created_at_iso|details_json``.
  The separator is ASCII Unit Separator (0x1f) so it can't appear in
  any reasonable field value.

GDPR erasure (P5.5): replaces user_id with SHA-256(user_id) and
NULLs out PII inside details_json BEFORE the chain hash is computed.
After erasure, the chain re-verifies because the same canonical
encoding produces the same hash (we never store the plaintext
user_id anywhere except the now-NULL user_id column itself).
"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable, Mapping

GENESIS = "GENESIS"
# Per-tenant chain genesis prefix (design §4). The org UUID is appended
# without a separator so verifiers can split at the first non-GENESIS-T-
# character and extract the org_id.
GENESIS_TENANT_PREFIX = "GENESIS-T-"
_SEP = b"\x1f"


def tenant_genesis(org_id: str) -> str:
    """Return the per-tenant genesis sentinel for ``org_id``.

    Making this a function (not a constant) ensures the sentinel is
    always derived from the org's UUID, which:
    1. Makes the chain-mode discriminator visible in the row data.
    2. Prevents cross-chain hash collisions (a per-tenant chain segment
       cannot be spliced into the shared chain — the first row's
       prev_hash wouldn't match GENESIS).
    3. Makes log-grep trivial: ``SELECT id FROM audit_logs WHERE
       prev_hash LIKE 'GENESIS-T-%'``.
    """
    return f"{GENESIS_TENANT_PREFIX}{org_id}"


def expected_genesis_for(org_id: str | None, chain_mode: str = "shared") -> str:
    """Return the expected genesis sentinel for a given org + chain mode.

    ``chain_mode='tenant'`` returns the per-tenant sentinel (design §4).
    Any other value (including ``'shared'``) returns the shared sentinel.
    Used by verifiers that need to determine the first row's expected
    ``prev_hash`` from the row's metadata.
    """
    if chain_mode == "tenant" and org_id:
        return tenant_genesis(org_id)
    return GENESIS


def _canonical(row: Mapping) -> bytes:
    """Stable bytes representation of a row's auditable content.

    The canonical encoding is order-stable and includes every field
    that's protected by the chain. Adding a field here = chain
    re-verification breaks for older rows; treat as a forward-only
    schema change requiring a migration.
    """
    return _SEP.join(
        [
            (row["id"] or "").encode("utf-8"),
            (row["action"] or "").encode("utf-8"),
            (row.get("user_id") or "").encode("utf-8"),
            (row.get("org_id") or "").encode("utf-8"),
            (row.get("resource_type") or "").encode("utf-8"),
            (row.get("resource_id") or "").encode("utf-8"),
            _iso(row.get("created_at")).encode("utf-8"),
            (row.get("details_json") or "").encode("utf-8"),
        ]
    )


def _iso(value) -> str:
    """Render a datetime / str / None as a stable ISO-8601 string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.isoformat()


def compute_hash(prev_hash: str | None, row: Mapping) -> str:
    """Return the SHA-256 hex of this row's content chained to prev_hash.

    ``prev_hash`` of ``None`` is treated as the genesis sentinel.
    """
    prev = (prev_hash or GENESIS).encode("utf-8")
    digest = hashlib.sha256(prev + _SEP + _canonical(row)).hexdigest()
    return digest


def verify_chain(rows: Iterable[Mapping]) -> tuple[bool, int | None, str | None]:
    """Verify that every row's prev_hash matches the chained hash.

    Returns ``(ok, first_bad_index, reason)``:
      - ok=True, first_bad_index=None, reason=None when the chain
        verifies end-to-end.
      - ok=False with the 0-based index of the first row that fails
        + a human-readable reason.

    Rows must be ordered by ascending ``created_at`` (or any total
    order — the chain is order-sensitive).
    """
    prev_hash: str | None = None
    rows_list = list(rows)
    for i, row in enumerate(rows_list):
        expected_prev = GENESIS if i == 0 else prev_hash
        stored_prev = row.get("prev_hash") or GENESIS
        if stored_prev != expected_prev:
            return (
                False,
                i,
                f"row[{i}].prev_hash={stored_prev!r} != expected {expected_prev!r}",
            )
        # Compute THIS row's hash for the next iteration's prev.
        prev_hash = compute_hash(stored_prev, row)
    return True, None, None


def row_as_mapping(audit_row) -> dict:
    """Convert an AuditLog ORM instance to the dict shape compute_hash expects."""
    return {
        "id": audit_row.id,
        "action": audit_row.action,
        "user_id": audit_row.user_id,
        "org_id": audit_row.org_id,
        "resource_type": audit_row.resource_type,
        "resource_id": audit_row.resource_id,
        "created_at": audit_row.created_at,
        "details_json": audit_row.details_json,
        "prev_hash": audit_row.prev_hash,
    }


# ---------------------------------------------------------------------------
# Per-tenant chain helpers (v1.2 #208)
# ---------------------------------------------------------------------------


def compute_tenant_hash(prev_hash: str | None, row: Mapping, org_id: str) -> str:
    """Return SHA-256 of this row chained to ``prev_hash``, domain-separated
    by ``org_id``.

    Domain separation prevents cross-tenant chain confusion: tenant A's
    chain head cannot be fed into tenant B's chain as a valid ``prev_hash``.
    The ``org_id`` is already part of ``_canonical(row)`` (the row's
    ``org_id`` field is included), so the salt adds no new entropy — it is
    a structural discriminator, not a secret.

    Backward compat: callers without per-tenant mode continue using
    ``compute_hash``; this function is only reached when the write path has
    confirmed ``isolation_mode='isolated'`` for the row's org.
    """
    # The per-tenant sentinel uses the org_id prefix (design §4) so the
    # genesis row's hash is discriminated by org even without explicit salt.
    # We still include org_id in the domain separator for defense-in-depth.
    prev = (prev_hash or tenant_genesis(org_id)).encode("utf-8")
    domain = f"tenant:{org_id}".encode()
    digest = hashlib.sha256(domain + _SEP + prev + _SEP + _canonical(row)).hexdigest()
    return digest


def verify_tenant_chain(
    rows: Iterable[Mapping],
    org_id: str,
) -> tuple[bool, int | None, str | None]:
    """Verify a per-tenant chain segment.

    Same contract as ``verify_chain`` but uses the per-tenant genesis
    sentinel and ``compute_tenant_hash`` for domain separation.

    A cross-tenant replay attack (injecting tenant A's rows into tenant B's
    chain) fails because the domain salt differs.

    Rows must be ordered by ascending ``created_at``.
    """
    prev_hash: str | None = None
    rows_list = list(rows)
    genesis = tenant_genesis(org_id)
    for i, row in enumerate(rows_list):
        expected_prev = genesis if i == 0 else prev_hash
        stored_prev = row.get("prev_hash") or genesis
        if stored_prev != expected_prev:
            return (
                False,
                i,
                f"row[{i}].prev_hash={stored_prev!r} != expected {expected_prev!r}",
            )
        prev_hash = compute_tenant_hash(stored_prev, row, org_id)
    return True, None, None


# CLI-friendly export helper. Used by the export endpoint AND the
# external verify script (so the same canonical encoding flows
# through both paths — the verifier can be run against an exported
# JSON dump in an air-gapped environment).
def serialize_for_export(audit_row) -> dict:
    """Stable JSON-serializable shape for /api/audit/export?format=json."""
    d = row_as_mapping(audit_row)
    d["created_at"] = _iso(d["created_at"])
    if d.get("details_json"):
        try:
            d["details"] = json.loads(d["details_json"])
        except (json.JSONDecodeError, TypeError):
            d["details"] = None
    return d
