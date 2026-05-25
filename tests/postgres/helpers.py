"""Utilities shared across P1 Postgres acceptance tests.

Tests prefer a `DATABASE_URL` env var pointing at a live Postgres (CI
provides this via service container). Locally, the same env var points
at an existing dev Postgres, or tests skip cleanly when neither is set.
"""

from __future__ import annotations

import os
import shutil
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
    """True if alembic CLI is on PATH (installed via requirements.txt)."""
    return shutil.which("alembic") is not None


def run_alembic(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run `alembic <args>` from the web-server dir; return CompletedProcess."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        ["alembic", *args],
        cwd=WEB_SERVER_ROOT,
        capture_output=True,
        text=True,
        env=full_env,
        timeout=120,
    )
