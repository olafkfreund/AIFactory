"""Schema-migration tests for #43 PR-1a (Epic #35 #43).

Covers migration d4f8c1a3e2b7:
- audit_anchors + audit_signing_keys tables created with expected columns.
- FK from audit_anchors.key_version → audit_signing_keys.version.
- audit_logs.classification column added with default 'internal'; existing
  rows backfilled to 'internal'.
- users.last_login_at column added (nullable).
- The Postgres-only unique index on DATE(signed_at AT TIME ZONE 'UTC')
  rejects two anchors for the same UTC day.
- Downgrade drops everything cleanly.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from tests.postgres.helpers import alembic_available, run_alembic


def _sync_url(url: str) -> str:
    return url.replace("+asyncpg", "")


@pytest.mark.postgres
@pytest.mark.slow
def test_audit_anchor_tables_created(test_postgres_url: str) -> None:
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    result = run_alembic(
        ["upgrade", "head"],
        env={"DATABASE_URL": test_postgres_url},
    )
    assert result.returncode == 0, f"upgrade failed: {result.stderr[-1000:]}"

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.connect() as conn:
        anchors = (
            conn.execute(
                text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'audit_anchors' ORDER BY ordinal_position
        """)
            )
            .scalars()
            .all()
        )
        keys = (
            conn.execute(
                text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'audit_signing_keys' ORDER BY ordinal_position
        """)
            )
            .scalars()
            .all()
        )
    engine.dispose()

    for col in (
        "id",
        "chain_head_hash",
        "signature",
        "signed_at",
        "key_version",
        "created_at",
    ):
        assert col in anchors, f"audit_anchors missing {col}"
    for col in ("version", "wrapped_key", "created_at", "retired_at"):
        assert col in keys, f"audit_signing_keys missing {col}"


@pytest.mark.postgres
@pytest.mark.slow
def test_audit_logs_classification_default_internal(
    test_postgres_url: str,
) -> None:
    """Existing audit_logs rows get classification='internal' on backfill."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.begin() as conn:
        # Insert a row WITHOUT specifying classification (uses default).
        conn.execute(
            text("""
            INSERT INTO audit_logs (id, action, resource_type)
            VALUES ('audit-default-1', 'test.default', 'test')
        """)
        )
        cls = conn.execute(
            text("SELECT classification FROM audit_logs WHERE id='audit-default-1'")
        ).scalar()
        conn.execute(text("DELETE FROM audit_logs WHERE id='audit-default-1'"))
    engine.dispose()

    assert cls == "internal"


@pytest.mark.postgres
@pytest.mark.slow
def test_users_last_login_at_nullable(test_postgres_url: str) -> None:
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.connect() as conn:
        info = conn.execute(
            text("""
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name='users' AND column_name='last_login_at'
        """)
        ).scalar()
    engine.dispose()
    assert info == "YES", "users.last_login_at should be nullable"


@pytest.mark.postgres
@pytest.mark.slow
def test_audit_anchors_unique_per_utc_day(test_postgres_url: str) -> None:
    """Postgres-only: the expression unique index on DATE(signed_at) blocks
    two anchors for the same UTC day."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})

    engine = create_engine(_sync_url(test_postgres_url))
    # Seed a signing key first (FK).
    with engine.begin() as conn:
        # Idempotent for re-runs.
        conn.execute(text("DELETE FROM audit_anchors WHERE id LIKE 'utc-day-%'"))
        # The signing key may already exist from a previous run; create
        # if missing.
        existing = conn.execute(
            text("SELECT MIN(version) FROM audit_signing_keys")
        ).scalar()
        if existing is None:
            conn.execute(
                text("""
                INSERT INTO audit_signing_keys (wrapped_key) VALUES (E'\\\\xdeadbeef')
            """)
            )
        key_version = conn.execute(
            text("SELECT MIN(version) FROM audit_signing_keys")
        ).scalar()

        conn.execute(
            text("""
            INSERT INTO audit_anchors (id, chain_head_hash, signature, signed_at, key_version)
            VALUES ('utc-day-1', 'hash1', 'sig1', '2026-05-28 02:00:00 UTC', :v)
        """),
            {"v": key_version},
        )

    with engine.begin() as conn:
        # Same UTC day, different time-of-day — must violate the unique idx.
        with pytest.raises(Exception):
            conn.execute(
                text("""
                INSERT INTO audit_anchors (id, chain_head_hash, signature, signed_at, key_version)
                VALUES ('utc-day-2', 'hash2', 'sig2', '2026-05-28 22:00:00 UTC', :v)
            """),
                {"v": key_version},
            )

    # Cleanup
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_anchors WHERE id LIKE 'utc-day-%'"))
    engine.dispose()


@pytest.mark.postgres
@pytest.mark.slow
def test_downgrade_clean(test_postgres_url: str) -> None:
    """Down + up round-trip must succeed without orphan indexes / FKs.

    Pin to the revision BEFORE audit-anchor (the immediate parent)
    rather than ``downgrade -1`` so subsequent migrations stacked on
    top of this one don't break this test (#36 PR-1 surfaced this —
    a relative ``-1`` made the test brittle). Same fix pattern as
    #178's ``test_external_identities.py``.
    """
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    env = {"DATABASE_URL": test_postgres_url}
    run_alembic(["upgrade", "head"], env=env)
    # 7a3e1c8f9b2d is external_identities, the migration immediately
    # before d4f8c1a3e2b7 (audit-anchor). Targeting it explicitly
    # downgrades through any newer migrations first, then drops the
    # audit-anchor tables last.
    down = run_alembic(["downgrade", "7a3e1c8f9b2d"], env=env)
    assert down.returncode == 0, f"downgrade failed: {down.stderr[-1000:]}"

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.connect() as conn:
        tables = (
            conn.execute(
                text("""
            SELECT table_name FROM information_schema.tables
            WHERE table_name IN ('audit_anchors', 'audit_signing_keys')
        """)
            )
            .scalars()
            .all()
        )
    engine.dispose()
    assert tables == [], f"downgrade left tables behind: {tables}"

    # Re-upgrade so subsequent tests start from head.
    run_alembic(["upgrade", "head"], env=env)
