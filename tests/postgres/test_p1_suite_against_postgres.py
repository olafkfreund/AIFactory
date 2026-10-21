"""P1.2 — full existing pytest suite passes against Postgres (not just SQLite)."""

import asyncio
import os
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tests.postgres.helpers import REPO_ROOT


async def _audit_table_exists(conn: AsyncConnection) -> bool:
    found = await conn.execute(text("SELECT to_regclass('public.audit_logs')"))
    return found.scalar_one() is not None


def _audit_ids(url: str) -> list[str]:
    """Ids of the audit rows present before the inner suite runs."""

    async def _q() -> list[str]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                if not await _audit_table_exists(conn):
                    return []  # a fresh database: the inner suite creates it
                rows = await conn.execute(text("SELECT id FROM audit_logs"))
                return [r[0] for r in rows]
        finally:
            await engine.dispose()

    return asyncio.run(_q())


def _delete_audit_rows_except(url: str, keep: list[str]) -> None:
    """Remove the audit rows the inner suite wrote into this shared database.

    The inner run exercises the REST task routes with APP_DISABLE_AUTH=true,
    and those write real ``task.*`` audit rows keyed by composite task ids
    longer than 36 chars (#1466). Left behind, they make every later
    downgrade test hit the ``c1f5a3d7b924`` guard, which correctly refuses to
    narrow ``resource_id`` over them. The suite leaves the database as it
    found it; the guard itself is untouched.

    Keyed on the ids that existed beforehand, not on a timestamp:
    ``created_at`` is a naive UTC ``timestamp`` and the session time zone
    need not be UTC, so a clock window silently kept the new rows.
    """

    async def _d() -> None:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                if not await _audit_table_exists(conn):
                    return
                await conn.execute(
                    text("DELETE FROM audit_logs WHERE NOT (id = ANY(:keep))"),
                    {"keep": keep},
                )
        finally:
            await engine.dispose()

    asyncio.run(_d())


@pytest.mark.postgres
@pytest.mark.slow
def test_full_pytest_suite_passes_against_postgres(test_postgres_url: str) -> None:
    """P1.2 — the existing `pytest tests/ -m 'not slow'` suite runs green
    against a real Postgres (no SQLite-isms in queries/migrations).

    Run as a subprocess so the inner pytest gets a fresh module state with
    DATABASE_URL pointing at Postgres. Excludes -m postgres/-m slow to keep
    runtime under ~30s.
    """
    venv_python = REPO_ROOT / "apps" / "backend" / ".venv" / "bin" / "python3"
    if not venv_python.exists():
        pytest.skip(
            "backend venv not present — `uv pip install -r tests/requirements-test.txt`"
        )

    env = os.environ.copy()
    env["DATABASE_URL"] = test_postgres_url
    # Don't recursively trigger the postgres-acceptance suite or we get infinite recursion.
    env["TEST_POSTGRES_URL"] = ""
    # Mirror the `backend (ruff + pytest)` job: the full suite runs in
    # no-auth/dev mode (project/task authz, #319, is exercised by dedicated
    # authz tests, not the bare-app route tests like test_issue_232_regression).
    env["APP_DISABLE_AUTH"] = "true"

    existing = _audit_ids(test_postgres_url)
    try:
        result = subprocess.run(
            [
                str(venv_python),
                "-m",
                "pytest",
                "tests/",
                "-m",
                "not slow and not postgres",
                "-q",
                "--tb=short",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
    finally:
        _delete_audit_rows_except(test_postgres_url, existing)
    assert result.returncode == 0, (
        f"existing pytest suite failed against Postgres:\n"
        f"--- last 3000 chars of stdout ---\n{result.stdout[-3000:]}\n"
        f"--- last 2000 chars of stderr ---\n{result.stderr[-2000:]}"
    )
