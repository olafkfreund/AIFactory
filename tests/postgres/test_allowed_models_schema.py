"""Schema-migration tests for #38 PR-2a (Epic #35 #38).

Covers migration f9c2b1a4d8e7:
- organizations.allowed_models column created.
- Defaults to ['*'] on insert when not explicitly set.
- Accepts JSON array values (e.g. ['claude-*'], ['gpt-4o-mini']).
- Downgrade drops the column cleanly.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from tests.postgres.helpers import alembic_available, run_alembic


def _sync_url(url: str) -> str:
    return url.replace("+asyncpg", "")


@pytest.mark.postgres
@pytest.mark.slow
def test_allowed_models_column_created(test_postgres_url: str) -> None:
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    result = run_alembic(
        ["upgrade", "head"],
        env={"DATABASE_URL": test_postgres_url},
    )
    assert result.returncode == 0, f"upgrade failed: {result.stderr[-1000:]}"

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.connect() as conn:
        info = conn.execute(
            text("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'organizations'
              AND column_name = 'allowed_models'
        """)
        ).fetchone()
    engine.dispose()

    assert info is not None, "allowed_models column missing"
    # Postgres reports JSONB as 'jsonb'; SQLAlchemy's JSON type maps to JSONB.
    assert info[1] in ("jsonb", "json", "text"), f"unexpected data_type: {info[1]}"
    assert info[2] == "NO", "allowed_models should be NOT NULL"


@pytest.mark.postgres
@pytest.mark.slow
def test_allowed_models_defaults_to_wildcard(test_postgres_url: str) -> None:
    """An Organization row inserted without an explicit allowed_models
    gets ['*'] from the server_default (backward compat)."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO users (id, email, password_hash, role, is_active)
            VALUES ('u-am-1', 'am1@example.com', 'x', 'user', true)
            ON CONFLICT (id) DO NOTHING
        """)
        )
        conn.execute(
            text("""
            INSERT INTO organizations (id, name, slug, owner_id, plan)
            VALUES ('org-am-1', 'AM', 'am-1', 'u-am-1', 'free')
            ON CONFLICT (id) DO NOTHING
        """)
        )
        result = conn.execute(
            text("SELECT allowed_models FROM organizations WHERE id='org-am-1'")
        ).scalar()

        # Cleanup before assertions so test is idempotent.
        conn.execute(text("DELETE FROM organizations WHERE id='org-am-1'"))
        conn.execute(text("DELETE FROM users WHERE id='u-am-1'"))
    engine.dispose()

    # Postgres returns JSONB as a Python list/dict via psycopg2;
    # SQLite returns the raw JSON string.
    if isinstance(result, str):
        result = json.loads(result)
    assert result == ["*"]


@pytest.mark.postgres
@pytest.mark.slow
def test_allowed_models_accepts_explicit_array(test_postgres_url: str) -> None:
    """Operator-supplied allowlist persists + round-trips correctly."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO users (id, email, password_hash, role, is_active)
            VALUES ('u-am-2', 'am2@example.com', 'x', 'user', true)
        """)
        )
        conn.execute(
            text("""
            INSERT INTO organizations (id, name, slug, owner_id, plan,
                                       allowed_models)
            VALUES ('org-am-2', 'AM', 'am-2', 'u-am-2', 'free',
                    '["claude-*", "gpt-4o-mini"]'::jsonb)
        """)
        )
        result = conn.execute(
            text("SELECT allowed_models FROM organizations WHERE id='org-am-2'")
        ).scalar()

        # Cleanup.
        conn.execute(text("DELETE FROM organizations WHERE id='org-am-2'"))
        conn.execute(text("DELETE FROM users WHERE id='u-am-2'"))
    engine.dispose()

    if isinstance(result, str):
        result = json.loads(result)
    assert result == ["claude-*", "gpt-4o-mini"]


@pytest.mark.postgres
@pytest.mark.slow
def test_downgrade_drops_column(test_postgres_url: str) -> None:
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    env = {"DATABASE_URL": test_postgres_url}
    run_alembic(["upgrade", "head"], env=env)
    # Pin to the immediate parent (e7b3d2c1f0a5 — tenant_states) so
    # this test stays robust to future migrations stacking on top
    # (same fix pattern as #178/#181/#192).
    down = run_alembic(["downgrade", "e7b3d2c1f0a5"], env=env)
    assert down.returncode == 0, f"downgrade failed: {down.stderr[-1000:]}"

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.connect() as conn:
        cols = (
            conn.execute(
                text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='organizations' AND column_name='allowed_models'
        """)
            )
            .scalars()
            .all()
        )
    engine.dispose()

    assert cols == [], "downgrade left allowed_models column behind"

    # Re-upgrade for subsequent tests.
    run_alembic(["upgrade", "head"], env=env)
