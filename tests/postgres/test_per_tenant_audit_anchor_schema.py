"""Schema-migration tests for per-tenant audit-chain anchor (v1.2 #208).

Covers migration c3d7e8f1a2b4:
- audit_anchors.org_id column added (nullable FK).
- audit_signing_keys.org_id column added (nullable FK).
- tenant_audit_state table created with expected columns + constraints.
- Composite index audit_logs(org_id, created_at) created.
- Postgres-only per-(org_id, day) unique index on audit_anchors.
- Downgrade drops everything cleanly; parent revision pinned explicitly
  (lesson from PR #178/#181/#192/#194 — never use relative -1).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from tests.postgres.helpers import alembic_available, run_alembic

# The migration immediately before c3d7e8f1a2b4 (per-tenant anchor).
# Pinned explicitly — a relative downgrade (-1) breaks when later migrations
# are stacked on top of this one (lesson from #178).
# scim_groups (a1b2c3d4e5f6) is the actual head that c3d7e8f1a2b4 builds on.
_PARENT_REVISION = "a1b2c3d4e5f6"


def _sync_url(url: str) -> str:
    return url.replace("+asyncpg", "")


@pytest.mark.postgres
@pytest.mark.slow
def test_per_tenant_columns_created(test_postgres_url: str) -> None:
    """audit_anchors.org_id and audit_signing_keys.org_id are created."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    result = run_alembic(
        ["upgrade", "head"], env={"DATABASE_URL": test_postgres_url},
    )
    assert result.returncode == 0, f"upgrade failed: {result.stderr[-1000:]}"

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.connect() as conn:
        anchor_cols = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'audit_anchors' ORDER BY ordinal_position
        """)).scalars().all()
        key_cols = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'audit_signing_keys' ORDER BY ordinal_position
        """)).scalars().all()
        tas_cols = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'tenant_audit_state' ORDER BY ordinal_position
        """)).scalars().all()
    engine.dispose()

    assert "org_id" in anchor_cols, "audit_anchors missing org_id"
    assert "org_id" in key_cols, "audit_signing_keys missing org_id"

    for col in (
        "org_id", "chain_started_at", "current_head_hash",
        "last_anchor_at", "lifecycle", "created_at", "updated_at",
    ):
        assert col in tas_cols, f"tenant_audit_state missing {col}"


@pytest.mark.postgres
@pytest.mark.slow
def test_audit_logs_composite_index_created(test_postgres_url: str) -> None:
    """Composite index audit_logs(org_id, created_at) is present."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.connect() as conn:
        idx_exists = conn.execute(text("""
            SELECT 1 FROM pg_indexes
            WHERE tablename = 'audit_logs'
              AND indexname = 'ix_audit_logs_org_id_created_at'
        """)).scalar()
    engine.dispose()
    assert idx_exists == 1, "composite index ix_audit_logs_org_id_created_at not found"


@pytest.mark.postgres
@pytest.mark.slow
def test_per_tenant_anchor_unique_per_org_day(test_postgres_url: str) -> None:
    """Per-(org, day) unique constraint blocks two anchors for the same org+date."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})

    engine = create_engine(_sync_url(test_postgres_url))

    # Seed org + key.
    org_id = "00000000-0000-0000-0000-000000000042"
    user_id = "00000000-0000-0000-0000-000000000001"
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_anchors WHERE id LIKE 'tenant-day-%'"))
        # Ensure a user exists (required by organizations.owner_id FK).
        conn.execute(text("""
            INSERT INTO users (id, email, password_hash)
            VALUES (:uid, 'test@example.com', 'x')
            ON CONFLICT (id) DO NOTHING
        """), {"uid": user_id})
        # Ensure org exists (FK).
        conn.execute(text("""
            INSERT INTO organizations (id, name, slug, owner_id)
            VALUES (:id, 'test-org', 'tenant-anchor-test-slug', :uid)
            ON CONFLICT (id) DO NOTHING
        """), {"id": org_id, "uid": user_id})

        existing_key = conn.execute(text(
            "SELECT MIN(version) FROM audit_signing_keys WHERE org_id = :oid"
        ), {"oid": org_id}).scalar()
        if existing_key is None:
            conn.execute(text("""
                INSERT INTO audit_signing_keys (wrapped_key, org_id)
                VALUES (E'\\\\xdeadbeef', :oid)
            """), {"oid": org_id})
        key_ver = conn.execute(text(
            "SELECT MIN(version) FROM audit_signing_keys WHERE org_id = :oid"
        ), {"oid": org_id}).scalar()

        conn.execute(text("""
            INSERT INTO audit_anchors (id, chain_head_hash, signature, signed_at, key_version, org_id)
            VALUES ('tenant-day-1', 'hash1', 'sig1', '2026-05-29 02:00:00', :v, :oid)
        """), {"v": key_ver, "oid": org_id})

    with engine.begin() as conn:
        with pytest.raises(Exception):
            conn.execute(text("""
                INSERT INTO audit_anchors (id, chain_head_hash, signature, signed_at, key_version, org_id)
                VALUES ('tenant-day-2', 'hash2', 'sig2', '2026-05-29 22:00:00', :v, :oid)
            """), {"v": key_ver, "oid": org_id})

    # Cleanup.
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_anchors WHERE id LIKE 'tenant-day-%'"))
    engine.dispose()


@pytest.mark.postgres
@pytest.mark.slow
def test_shared_anchor_still_allowed_after_migration(test_postgres_url: str) -> None:
    """Shared-chain (NULL org_id) anchors continue to work after migration."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_anchors WHERE id LIKE 'shared-test-%'"))
        existing = conn.execute(text(
            "SELECT MIN(version) FROM audit_signing_keys WHERE org_id IS NULL"
        )).scalar()
        if existing is None:
            conn.execute(text("""
                INSERT INTO audit_signing_keys (wrapped_key)
                VALUES (E'\\\\xdeadbeef')
            """))
        key_ver = conn.execute(text(
            "SELECT MIN(version) FROM audit_signing_keys WHERE org_id IS NULL"
        )).scalar()

        conn.execute(text("""
            INSERT INTO audit_anchors
              (id, chain_head_hash, signature, signed_at, key_version, org_id)
            VALUES ('shared-test-1', 'hash1', 'sig1', '2026-05-30 00:00:00', :v, NULL)
        """), {"v": key_ver})

        org_id = conn.execute(text(
            "SELECT org_id FROM audit_anchors WHERE id='shared-test-1'"
        )).scalar()

        conn.execute(text("DELETE FROM audit_anchors WHERE id LIKE 'shared-test-%'"))

    engine.dispose()
    assert org_id is None, "shared anchor should have NULL org_id"


@pytest.mark.postgres
@pytest.mark.slow
def test_downgrade_drops_columns(test_postgres_url: str) -> None:
    """Downgrade to parent revision drops all v1.2 columns + table cleanly.

    Parent revision is pinned explicitly as ``f9c2b1a4d8e7`` — NOT ``-1`` —
    so this test stays correct when later migrations are added on top.
    """
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    env = {"DATABASE_URL": test_postgres_url}
    run_alembic(["upgrade", "head"], env=env)

    down = run_alembic(["downgrade", _PARENT_REVISION], env=env)
    assert down.returncode == 0, f"downgrade failed: {down.stderr[-1000:]}"

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.connect() as conn:
        # tenant_audit_state table should be gone.
        tables = conn.execute(text("""
            SELECT table_name FROM information_schema.tables
            WHERE table_name = 'tenant_audit_state'
        """)).scalars().all()

        # org_id columns should be gone from audit_anchors + audit_signing_keys.
        anchor_cols = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'audit_anchors'
        """)).scalars().all()
        key_cols = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'audit_signing_keys'
        """)).scalars().all()
    engine.dispose()

    assert tables == [], f"downgrade left tenant_audit_state behind: {tables}"
    assert "org_id" not in anchor_cols, "downgrade left org_id on audit_anchors"
    assert "org_id" not in key_cols, "downgrade left org_id on audit_signing_keys"

    # Re-upgrade so subsequent tests start from head.
    run_alembic(["upgrade", "head"], env=env)
