"""trusted_contracts exists at Alembic head on Postgres (#1667, test 7)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from tests.postgres.helpers import alembic_available, run_alembic, sync_url


@pytest.mark.postgres
@pytest.mark.slow
def test_trusted_contracts_table_at_head(test_postgres_url: str) -> None:
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")
    result = run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})
    assert result.returncode == 0, f"upgrade failed: {result.stderr[-1000:]}"

    engine = create_engine(sync_url(test_postgres_url))
    with engine.connect() as conn:
        cols = {
            r[0]: r[1]
            for r in conn.execute(
                text(
                    "SELECT column_name, character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'trusted_contracts'"
                )
            )
        }
    engine.dispose()

    assert set(cols) == {
        "spec_key",
        "spec_id",
        "contract",
        "build_isolation",
        "created_at",
        "updated_at",
    }
    assert cols["spec_key"] == 64
    assert cols["build_isolation"] == 16
