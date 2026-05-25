"""P1.3 / P1.4 — Alembic baseline migration + idempotent upgrade + autoApply modes."""

import os
import subprocess
from pathlib import Path

import pytest

from tests.postgres.helpers import (
    WEB_SERVER_ROOT,
    alembic_available,
    run_alembic,
)


@pytest.mark.postgres
@pytest.mark.slow
def test_alembic_config_present() -> None:
    """P1.3 — alembic.ini and versions/ directory exist under apps/web-server/."""
    assert (WEB_SERVER_ROOT / "alembic.ini").exists(), \
        f"{WEB_SERVER_ROOT / 'alembic.ini'} missing"
    assert (WEB_SERVER_ROOT / "server" / "database" / "alembic" / "versions").is_dir(), \
        "Alembic versions/ directory missing"


@pytest.mark.postgres
@pytest.mark.slow
def test_alembic_upgrade_head_on_empty_postgres(test_postgres_url: str) -> None:
    """P1.3 — `alembic upgrade head` creates all tables on a fresh Postgres."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    result = run_alembic(
        ["upgrade", "head"],
        env={"DATABASE_URL": test_postgres_url},
    )
    assert result.returncode == 0, \
        f"alembic upgrade head failed:\n{result.stderr[-2000:]}"


@pytest.mark.postgres
@pytest.mark.slow
def test_alembic_upgrade_idempotent(test_postgres_url: str) -> None:
    """P1.3 — running upgrade head twice is a no-op the second time."""
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    env = {"DATABASE_URL": test_postgres_url}
    first = run_alembic(["upgrade", "head"], env=env)
    assert first.returncode == 0, f"first upgrade failed: {first.stderr[-1000:]}"

    second = run_alembic(["upgrade", "head"], env=env)
    assert second.returncode == 0, \
        f"second upgrade was not idempotent: {second.stderr[-1000:]}"


def _init_db_subprocess(test_postgres_url: str, auto_apply: bool) -> subprocess.CompletedProcess:
    """Invoke server.database.engine.init_db() in a fresh subprocess.

    Subprocess isolation keeps each test's module state clean (DATABASE_URL
    is read at module import time inside engine.py).
    """
    import sys

    code = (
        "import asyncio, sys, os; "
        "sys.path.insert(0, 'apps/web-server'); "
        "from server.database.engine import init_db; "
        "asyncio.run(init_db()); "
        "print('init_db OK')"
    )
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": test_postgres_url,
        # Settings class has env_prefix="APP_" — the actual env var that
        # MIGRATIONS_AUTO_APPLY: bool reads from is APP_MIGRATIONS_AUTO_APPLY.
        "APP_MIGRATIONS_AUTO_APPLY": "true" if auto_apply else "false",
        "APP_DISABLE_AUTH": "true",
        "GRAPHITI_ENABLED": "false",
    })
    repo_root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _drop_schema(test_postgres_url: str) -> None:
    """Drop + recreate the `public` schema, leaving the DB empty."""
    import asyncio
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _drop():
        eng = create_async_engine(test_postgres_url)
        try:
            async with eng.connect() as conn:
                # AUTOCOMMIT so DROP SCHEMA isn't held in a transaction
                # that conflicts with our own session.
                await conn.execute(text("COMMIT"))
                await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
                await conn.execute(text("CREATE SCHEMA public"))
                await conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
                await conn.commit()
        finally:
            await eng.dispose()

    asyncio.run(_drop())


@pytest.mark.postgres
@pytest.mark.slow
def test_app_boot_with_autoapply_true_runs_migrations(test_postgres_url: str) -> None:
    """P1.4 — MIGRATIONS_AUTO_APPLY=true causes init_db() to upgrade DB schema
    automatically on an empty Postgres."""
    _drop_schema(test_postgres_url)
    result = _init_db_subprocess(test_postgres_url, auto_apply=True)
    assert result.returncode == 0, (
        f"init_db exit {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr[-2000:]}"
    )
    assert "init_db OK" in result.stdout, "init_db did not complete cleanly"


@pytest.mark.postgres
@pytest.mark.slow
def test_app_boot_with_autoapply_false_fails_fast(test_postgres_url: str) -> None:
    """P1.4 — MIGRATIONS_AUTO_APPLY=false against an un-migrated DB raises
    RuntimeError fast (no schema bring-up; expects out-of-band Helm Job)."""
    _drop_schema(test_postgres_url)
    result = _init_db_subprocess(test_postgres_url, auto_apply=False)
    assert result.returncode != 0, \
        f"expected non-zero exit; got 0:\n{result.stdout}"
    combined = result.stdout + result.stderr
    assert "RuntimeError" in combined or "schema is at" in combined, \
        f"expected RuntimeError about schema mismatch:\n{combined[-2000:]}"


@pytest.mark.postgres
@pytest.mark.slow
@pytest.mark.skip(reason="P1.5 implementation pending: app-side UUIDs + no CREATE EXTENSION")
def test_alembic_succeeds_without_create_extension_privilege(test_postgres_url: str) -> None:
    """P1.5 — Alembic upgrade succeeds against a role with NO CREATE EXTENSION grant.

    Mirrors the bank scenario: app role gets DML + schema ownership but cannot
    install pgcrypto/uuid-ossp. UUIDs must therefore be generated app-side.
    """
    if not alembic_available():
        pytest.skip("alembic CLI not on PATH")

    # Implementation note (P1.5): this test runs the migration as a role that
    # lacks CREATE EXTENSION. The test setup creates such a role + grants only
    # the minimum privileges from guides/deployment/postgres-privileges.md.
    pytest.fail("P1.5 setup not landed yet")
