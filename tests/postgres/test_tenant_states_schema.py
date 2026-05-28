"""Schema-migration tests for #36 PR-1 (Epic #35 #36).

Covers migration e7b3d2c1f0a5:
- tenant_states table created with expected columns + FK CASCADE.
- organizations.tenant_namespace + organizations.deleted_at columns added.
- isolation_mode defaults to 'shared'.
- ON DELETE CASCADE on tenant_states.org_id wipes the row when the
  org row is deleted (prevents FK orphans).
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
def test_tenant_states_table_created(test_postgres_url: str) -> None:
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    result = run_alembic(
        ["upgrade", "head"], env={"DATABASE_URL": test_postgres_url},
    )
    assert result.returncode == 0, (
        f"upgrade failed: {result.stderr[-1000:]}"
    )

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.connect() as conn:
        cols = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'tenant_states' ORDER BY ordinal_position
        """)).scalars().all()
    engine.dispose()

    for col in (
        "org_id", "isolation_mode", "namespace_name", "service_account",
        "iam_role_arn", "vault_policy_name", "reconciled_at",
        "reconcile_error", "created_at", "updated_at",
    ):
        assert col in cols, f"tenant_states missing {col}"


@pytest.mark.postgres
@pytest.mark.slow
def test_organizations_new_columns(test_postgres_url: str) -> None:
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.connect() as conn:
        cols = conn.execute(text("""
            SELECT column_name, is_nullable FROM information_schema.columns
            WHERE table_name = 'organizations'
              AND column_name IN ('tenant_namespace', 'deleted_at')
        """)).fetchall()
    engine.dispose()

    by_name = {c[0]: c[1] for c in cols}
    assert by_name.get("tenant_namespace") == "YES"
    assert by_name.get("deleted_at") == "YES"


@pytest.mark.postgres
@pytest.mark.slow
def test_isolation_mode_defaults_to_shared(test_postgres_url: str) -> None:
    """A new tenant_states row without an explicit isolation_mode gets
    'shared' from the server_default."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.begin() as conn:
        # Need a User + Org for the FK.
        conn.execute(text("""
            INSERT INTO users (id, email, password_hash, role, is_active)
            VALUES ('u-iso-test', 'iso@example.com', 'x', 'user', true)
            ON CONFLICT (id) DO NOTHING
        """))
        conn.execute(text("""
            INSERT INTO organizations (id, name, slug, owner_id, plan)
            VALUES ('org-iso-test', 'Iso', 'iso-test', 'u-iso-test', 'free')
            ON CONFLICT (id) DO NOTHING
        """))
        # Insert with only the FK populated.
        conn.execute(text("""
            INSERT INTO tenant_states (org_id) VALUES ('org-iso-test')
        """))
        mode = conn.execute(text(
            "SELECT isolation_mode FROM tenant_states WHERE org_id='org-iso-test'"
        )).scalar()

        # Cleanup.
        conn.execute(text("DELETE FROM tenant_states WHERE org_id='org-iso-test'"))
        conn.execute(text("DELETE FROM organizations WHERE id='org-iso-test'"))
        conn.execute(text("DELETE FROM users WHERE id='u-iso-test'"))
    engine.dispose()

    assert mode == "shared"


@pytest.mark.postgres
@pytest.mark.slow
def test_cascade_delete_wipes_tenant_state(test_postgres_url: str) -> None:
    """Deleting an Organization cascades to its tenant_states row.
    Prevents FK orphans after hard-delete."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO users (id, email, password_hash, role, is_active)
            VALUES ('u-cas-2', 'cas@example.com', 'x', 'user', true)
        """))
        conn.execute(text("""
            INSERT INTO organizations (id, name, slug, owner_id, plan)
            VALUES ('org-cas-2', 'Cas', 'cas-2', 'u-cas-2', 'free')
        """))
        conn.execute(text("""
            INSERT INTO tenant_states (org_id, isolation_mode)
            VALUES ('org-cas-2', 'isolated')
        """))

        conn.execute(text("DELETE FROM organizations WHERE id='org-cas-2'"))

        remaining = conn.execute(text(
            "SELECT COUNT(*) FROM tenant_states WHERE org_id='org-cas-2'"
        )).scalar()
        assert remaining == 0, (
            "CASCADE delete did not fire — orphan tenant_state left"
        )

        # Cleanup the user.
        conn.execute(text("DELETE FROM users WHERE id='u-cas-2'"))
    engine.dispose()


@pytest.mark.postgres
@pytest.mark.slow
def test_downgrade_drops_everything(test_postgres_url: str) -> None:
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    env = {"DATABASE_URL": test_postgres_url}
    run_alembic(["upgrade", "head"], env=env)
    # Pin to the immediate parent (d4f8c1a3e2b7 — audit_anchor) so
    # future migrations stacked on top don't break this test. Same
    # brittleness-fix pattern as #178/#181/#192. #38 PR-2a's
    # allowed_models migration is what broke the relative ``-1``.
    down = run_alembic(["downgrade", "d4f8c1a3e2b7"], env=env)
    assert down.returncode == 0, f"downgrade failed: {down.stderr[-1000:]}"

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.connect() as conn:
        tables = conn.execute(text("""
            SELECT table_name FROM information_schema.tables
            WHERE table_name = 'tenant_states'
        """)).scalars().all()
        # Organizations columns should also be dropped.
        cols = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='organizations'
              AND column_name IN ('tenant_namespace', 'deleted_at')
        """)).scalars().all()
    engine.dispose()

    assert tables == [], "downgrade left tenant_states behind"
    assert cols == [], "downgrade left Organization columns behind"

    # Re-upgrade for subsequent tests.
    run_alembic(["upgrade", "head"], env=env)
