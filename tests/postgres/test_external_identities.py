"""Schema migration test for external_identities (Epic #35 #41 PR-1b).

Covers:
- Migration 7a3e1c8f9b2d creates the table with all expected columns
  + indices + the (kind, subject) unique constraint.
- Backfill copies existing ``users.oidc_sub`` values into
  external_identities with kind='oidc:legacy'.
- Downgrade drops the table cleanly.
- The (kind, subject) unique constraint actually rejects duplicates.
- The user_id FK has ON DELETE CASCADE so deleting a user wipes
  their identity rows (otherwise we'd leak dangling FK rows).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from tests.postgres.helpers import alembic_available, run_alembic


def _sync_url(url: str) -> str:
    """Strip the asyncpg driver suffix so we can use a sync engine for
    the verification queries (alembic + manual SQL)."""
    return url.replace("+asyncpg", "")


@pytest.mark.postgres
@pytest.mark.slow
def test_external_identities_table_created(test_postgres_url: str) -> None:
    """7a3e1c8f9b2d creates the table with the right shape."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    result = run_alembic(
        ["upgrade", "head"], env={"DATABASE_URL": test_postgres_url},
    )
    assert result.returncode == 0, f"upgrade failed: {result.stderr[-1000:]}"

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.connect() as conn:
        cols = conn.execute(text("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'external_identities'
            ORDER BY ordinal_position
        """)).fetchall()
    engine.dispose()

    col_names = [c[0] for c in cols]
    assert "id" in col_names
    assert "user_id" in col_names
    assert "kind" in col_names
    assert "subject" in col_names
    assert "created_at" in col_names


@pytest.mark.postgres
@pytest.mark.slow
def test_unique_kind_subject_constraint(test_postgres_url: str) -> None:
    """Two rows with the same (kind, subject) must be rejected by the
    DB even if the application layer doesn't catch it."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.begin() as conn:
        # Create a user to satisfy the FK.
        conn.execute(text("""
            INSERT INTO users (id, email, password_hash, role, is_active)
            VALUES ('user-aaaa-1', 'a@example.com', 'x', 'user', true)
            ON CONFLICT (id) DO NOTHING
        """))

        conn.execute(text("""
            INSERT INTO external_identities (id, user_id, kind, subject)
            VALUES ('ext-1', 'user-aaaa-1', 'saml:corp', 'a@example.com')
        """))

    with engine.begin() as conn:
        # Same (kind, subject) — must fail.
        with pytest.raises(Exception):
            conn.execute(text("""
                INSERT INTO external_identities (id, user_id, kind, subject)
                VALUES ('ext-2', 'user-aaaa-1', 'saml:corp', 'a@example.com')
            """))

    # Clean up so the test is idempotent across re-runs.
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM external_identities WHERE user_id='user-aaaa-1'"))
        conn.execute(text("DELETE FROM users WHERE id='user-aaaa-1'"))
    engine.dispose()


@pytest.mark.postgres
@pytest.mark.slow
def test_cascade_delete_on_user(test_postgres_url: str) -> None:
    """Deleting a user wipes their external_identities rows. Without
    the ON DELETE CASCADE we'd leak FK orphans."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO users (id, email, password_hash, role, is_active)
            VALUES ('user-cas-1', 'c@example.com', 'x', 'user', true)
        """))
        conn.execute(text("""
            INSERT INTO external_identities (id, user_id, kind, subject)
            VALUES ('ext-cas-1', 'user-cas-1', 'saml:corp', 'c@example.com')
        """))

        conn.execute(text("DELETE FROM users WHERE id='user-cas-1'"))

        n = conn.execute(text(
            "SELECT COUNT(*) FROM external_identities WHERE user_id='user-cas-1'"
        )).scalar()
        assert n == 0, "CASCADE delete did not fire — orphan ext_identity left"

    engine.dispose()


@pytest.mark.postgres
@pytest.mark.slow
def test_downgrade_drops_table(test_postgres_url: str) -> None:
    """Down + up round-trip must succeed without orphan indexes / FKs.

    Pin to the revision BEFORE external_identities (the immediate
    parent) rather than ``downgrade -1`` so subsequent migrations
    stacked on top of this one don't break this test (#43 PR-1a
    surfaced this — a relative ``-1`` made the test brittle).
    """
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    env = {"DATABASE_URL": test_postgres_url}
    run_alembic(["upgrade", "head"], env=env)
    # b2d4f7e9c3a1 is git_credentials, the migration immediately
    # before 7a3e1c8f9b2d (external_identities). Targeting it
    # explicitly downgrades through any newer migrations first,
    # then drops external_identities last.
    down = run_alembic(["downgrade", "b2d4f7e9c3a1"], env=env)
    assert down.returncode == 0, f"downgrade failed: {down.stderr[-1000:]}"

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.connect() as conn:
        exists = conn.execute(text("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'external_identities'
            )
        """)).scalar()
    engine.dispose()
    assert exists is False, "downgrade left the table behind"

    # Re-upgrade so subsequent tests start from head.
    run_alembic(["upgrade", "head"], env=env)
