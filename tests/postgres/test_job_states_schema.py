"""Schema-migration tests for job_states (RFC-0016 #668).

Covers migration b7e1c9a4d2f3:
- job_states table created with expected columns + indexes.
- downgrade drops the table + indexes cleanly, then re-upgrades.

Marked ``postgres``/``slow`` — runs in the ``postgres-acceptance`` CI job
(``-m postgres``); skipped in the default fast gate.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from tests.postgres.helpers import alembic_available, run_alembic


def _sync_url(url: str) -> str:
    return url.replace("+asyncpg", "")


@pytest.mark.postgres
@pytest.mark.slow
def test_job_states_table_created(test_postgres_url: str) -> None:
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    result = run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})
    assert result.returncode == 0, f"upgrade failed: {result.stderr[-1000:]}"

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.connect() as conn:
        cols = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'job_states' ORDER BY ordinal_position
        """)).scalars().all()
        indexes = conn.execute(text("""
            SELECT indexname FROM pg_indexes WHERE tablename = 'job_states'
        """)).scalars().all()
    engine.dispose()

    for col in (
        "schema_version", "job_id", "correlation_key", "service", "kind",
        "lifecycle_state", "service_status", "phase", "attempt", "admission",
        "worker_ref", "spawn_args", "result", "usage", "error",
        "created_at", "updated_at", "ended_at",
    ):
        assert col in cols, f"job_states missing {col}"

    assert "ix_job_states_service_lifecycle" in indexes
    assert "ix_job_states_correlation_key" in indexes


@pytest.mark.postgres
@pytest.mark.slow
def test_job_states_downgrade_drops_table(test_postgres_url: str) -> None:
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    env = {"DATABASE_URL": test_postgres_url}
    run_alembic(["upgrade", "head"], env=env)
    # Pin to the immediate parent so future migrations stacked on top don't
    # break this test (same brittleness-fix pattern as the other suites).
    down = run_alembic(["downgrade", "f1a2b3c4d5e6"], env=env)
    assert down.returncode == 0, f"downgrade failed: {down.stderr[-1000:]}"

    engine = create_engine(_sync_url(test_postgres_url))
    with engine.connect() as conn:
        tables = conn.execute(text("""
            SELECT table_name FROM information_schema.tables
            WHERE table_name = 'job_states'
        """)).scalars().all()
    engine.dispose()
    assert tables == [], "downgrade left job_states behind"

    # Re-upgrade for subsequent tests.
    run_alembic(["upgrade", "head"], env=env)
