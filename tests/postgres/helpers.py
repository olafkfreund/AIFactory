"""Utilities shared across P1 Postgres acceptance tests.

Tests prefer a `DATABASE_URL` env var pointing at a live Postgres (CI
provides this via service container). Locally, the same env var points
at an existing dev Postgres, or tests skip cleanly when neither is set.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_SERVER_ROOT = REPO_ROOT / "apps" / "web-server"

# The single source of truth for the test Postgres URL. CI sets this to point
# at the postgres:15 / postgres:16 service container. Local devs can set it
# to a personal dev Postgres or leave it unset to skip P1 tests.
TEST_DATABASE_URL_ENV = "TEST_POSTGRES_URL"


def get_test_postgres_url() -> str | None:
    """Return the test Postgres URL, or None if not configured."""
    return os.environ.get(TEST_DATABASE_URL_ENV)


def postgres_reachable(url: str, timeout: float = 5.0) -> bool:
    """Cheap TCP probe — does the Postgres host:port accept connections?"""
    # Crude parsing: postgresql+asyncpg://user:pass@host:port/db
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if not parsed.hostname:
        return False
    port = parsed.port or 5432

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((parsed.hostname, port), timeout=1.0):
                return True
        except (ConnectionRefusedError, OSError):
            pass
        time.sleep(0.25)
    return False


def alembic_available() -> bool:
    """True if the alembic Python package is importable in this interpreter.

    We deliberately don't probe `shutil.which("alembic")` because the binary
    only appears on PATH when the venv is activated; pytest is usually
    invoked via `apps/backend/.venv/bin/pytest` which doesn't activate the
    venv shell environment. Importing the module is the right portability
    check.
    """
    try:
        import alembic  # noqa: F401

        return True
    except ImportError:
        return False


def run_alembic(
    args: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run alembic via the active Python interpreter (no PATH dependency).

    Uses `sys.executable -m alembic` rather than the `alembic` binary so
    the test runs with whichever venv is active — production CI, local
    backend venv, doesn't matter.
    """
    import sys

    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=WEB_SERVER_ROOT,
        capture_output=True,
        text=True,
        env=full_env,
        timeout=120,
    )


def sync_url(url: str) -> str:
    """The async test URL as a SYNC one, with the driver named explicitly.

    The column-migration tests seed pre-migration rows through a sync
    ``create_engine``, so they need the async URL without ``+asyncpg``.

    Naming ``+psycopg2`` rather than dropping the driver entirely is the point
    (#1612): a bare ``postgresql://`` delegates the choice to SQLAlchemy, whose
    default moved from psycopg2 to psycopg v3 in 2.1 while only
    ``psycopg2-binary`` is installed (``tests/requirements-test.txt``). Since
    ``apps/web-server/requirements.txt`` declares a floor and no ceiling, CI took
    2.1 as soon as it shipped and every postgres test that opened a connection
    died on ``ModuleNotFoundError: No module named 'psycopg'`` — while a local
    venv on 2.0 kept passing. Say which driver the suite means and no library's
    default can decide whether CI is green.
    """
    return url.replace("+asyncpg", "+psycopg2")
