"""Project identity columns, teams tables and slug uniqueness on Postgres (#2157)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from tests.postgres.helpers import alembic_available, run_alembic, sync_url


@pytest.mark.postgres
@pytest.mark.slow
def test_identity_schema_and_slug_unique_per_org(test_postgres_url: str) -> None:
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")
    result = run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})
    assert result.returncode == 0, f"upgrade failed: {result.stderr[-1000:]}"

    engine = create_engine(sync_url(test_postgres_url))
    with engine.begin() as conn:
        cols = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='projects'"
                )
            )
        }
        assert {"slug", "repo_identity", "team_id", "archived_at"} <= cols
        assert conn.execute(text("SELECT count(*) FROM teams")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM team_memberships")).scalar() == 0
        conn.execute(text("DELETE FROM projects WHERE id IN ('pg-i1','pg-i2')"))
        conn.execute(
            text(
                "INSERT INTO users(id,email,password_hash,role,is_active) "
                "VALUES ('pg-iu','pg-iu@x.test','x','user',true) ON CONFLICT DO NOTHING"
            )
        )
        conn.execute(
            text(
                "INSERT INTO organizations(id,name,slug,owner_id,plan,allowed_models) "
                "VALUES ('pg-io','o','pg-io','pg-iu','free','[\"*\"]') ON CONFLICT DO NOTHING"
            )
        )
        conn.execute(
            text(
                "INSERT INTO projects(id,org_id,name,path,slug) "
                "VALUES ('pg-i1','pg-io','n','/tmp/a','same')"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO projects(id,org_id,name,path,slug) "
                "VALUES ('pg-i2','pg-io','n','/tmp/b','same')"
            )
        )
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM projects WHERE id='pg-i1'"))
