"""Unit tests for per-tenant chain helpers (v1.2 #208).

Covers:
- compute_tenant_hash produces different values from compute_hash for the
  same row (domain separation).
- Same row + different tenant_id → different hash (cross-tenant isolation).
- verify_tenant_chain verifies a correctly-built per-tenant chain.
- Cross-tenant replay is rejected: tenant A's chain head can't validate
  tenant B's row.
- expected_genesis_for returns the correct sentinel by chain_mode.
"""

from __future__ import annotations

from server.services.audit_chain import (
    GENESIS,
    GENESIS_TENANT_PREFIX,
    compute_hash,
    compute_tenant_hash,
    expected_genesis_for,
    tenant_genesis,
    verify_chain,
    verify_tenant_chain,
)

ORG_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ORG_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

_ROW = {
    "id": "row-001",
    "action": "user.login",
    "user_id": "user-1",
    "org_id": ORG_A,
    "resource_type": "session",
    "resource_id": None,
    "created_at": "2026-05-29T00:00:00",
    "details_json": "{}",
    "prev_hash": None,
    "classification": "internal",
}


def _make_row(idx: int, prev_hash: str, org_id: str) -> dict:
    return {
        **_ROW,
        "id": f"row-{idx:04d}",
        "org_id": org_id,
        "prev_hash": prev_hash,
    }


class TestTenantGenesis:
    def test_genesis_prefix(self) -> None:
        sentinel = tenant_genesis(ORG_A)
        assert sentinel.startswith(GENESIS_TENANT_PREFIX)
        assert ORG_A in sentinel

    def test_different_orgs_different_sentinels(self) -> None:
        assert tenant_genesis(ORG_A) != tenant_genesis(ORG_B)

    def test_expected_genesis_for_shared(self) -> None:
        assert expected_genesis_for(ORG_A, "shared") == GENESIS

    def test_expected_genesis_for_tenant(self) -> None:
        assert expected_genesis_for(ORG_A, "tenant") == tenant_genesis(ORG_A)

    def test_expected_genesis_for_no_org(self) -> None:
        assert expected_genesis_for(None, "tenant") == GENESIS


class TestComputeTenantHash:
    def test_differs_from_shared_hash(self) -> None:
        """Per-tenant hash must differ from the shared hash for the same inputs."""
        shared = compute_hash(GENESIS, _ROW)
        per_tenant = compute_tenant_hash(tenant_genesis(ORG_A), _ROW, ORG_A)
        assert shared != per_tenant, (
            "compute_tenant_hash must produce a different value from compute_hash "
            "(domain separation failure)"
        )

    def test_different_org_different_hash(self) -> None:
        """Same row, different org_id → different hash."""
        hash_a = compute_tenant_hash(tenant_genesis(ORG_A), _ROW, ORG_A)
        hash_b = compute_tenant_hash(tenant_genesis(ORG_B), _ROW, ORG_B)
        assert hash_a != hash_b

    def test_deterministic(self) -> None:
        h1 = compute_tenant_hash(tenant_genesis(ORG_A), _ROW, ORG_A)
        h2 = compute_tenant_hash(tenant_genesis(ORG_A), _ROW, ORG_A)
        assert h1 == h2

    def test_none_prev_hash_uses_genesis_sentinel(self) -> None:
        """None prev_hash should behave as the tenant genesis sentinel."""
        h_none = compute_tenant_hash(None, _ROW, ORG_A)
        h_genesis = compute_tenant_hash(tenant_genesis(ORG_A), _ROW, ORG_A)
        assert h_none == h_genesis


class TestVerifyTenantChain:
    def _build_chain(self, org_id: str, length: int) -> list[dict]:
        """Build a correctly-linked per-tenant chain of ``length`` rows."""
        rows = []
        prev = tenant_genesis(org_id)
        for i in range(length):
            row = _make_row(i, prev, org_id)
            prev = compute_tenant_hash(prev, row, org_id)
            rows.append(row)
        return rows

    def test_happy_path(self) -> None:
        rows = self._build_chain(ORG_A, 5)
        ok, idx, reason = verify_tenant_chain(rows, ORG_A)
        assert ok, f"expected chain to verify; first failure at row[{idx}]: {reason}"

    def test_empty_chain_verifies(self) -> None:
        ok, idx, reason = verify_tenant_chain([], ORG_A)
        assert ok

    def test_single_row_verifies(self) -> None:
        rows = self._build_chain(ORG_A, 1)
        ok, idx, reason = verify_tenant_chain(rows, ORG_A)
        assert ok

    def test_tampered_row_detected(self) -> None:
        rows = self._build_chain(ORG_A, 3)
        # Tamper the second row's content.
        rows[1] = {**rows[1], "action": "tampered.action"}
        ok, idx, _ = verify_tenant_chain(rows, ORG_A)
        assert not ok
        # The tampered row itself or the row after it should fail.
        assert idx is not None

    def test_cross_tenant_replay_rejected(self) -> None:
        """Tenant A's chain head must not validate a row that names org B."""
        rows_a = self._build_chain(ORG_A, 3)
        # Inject rows from org A into org B's verifier.
        ok, idx, reason = verify_tenant_chain(rows_a, ORG_B)
        # The first row's prev_hash is GENESIS-T-ORG_A which doesn't match
        # the expected GENESIS-T-ORG_B sentinel.
        assert not ok, (
            "cross-tenant replay: org B's verifier accepted org A's chain — "
            "domain separation failure"
        )

    def test_wrong_genesis_sentinel_rejected(self) -> None:
        """A chain starting with the SHARED genesis fails tenant verification."""
        # Build a row with the shared-chain genesis as prev_hash.
        row = _make_row(0, GENESIS, ORG_A)
        ok, idx, _ = verify_tenant_chain([row], ORG_A)
        assert not ok, "shared-chain genesis should not be accepted by tenant verifier"

    def test_verify_chain_shared_unchanged(self) -> None:
        """The shared-chain verify_chain function still works after v1.2 changes."""
        rows = []
        prev = GENESIS
        for i in range(3):
            row = _make_row(i, prev, ORG_A)
            prev = compute_hash(prev, row)
            rows.append(row)
        ok, _, _ = verify_chain(rows)
        assert ok, "shared verify_chain must still work after v1.2 changes"
