"""Regression tests for boot migration locking."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "web-server"))

from server.database import engine as engine_mod  # noqa: E402


def test_sqlite_init_db_does_not_use_advisory_lock(tmp_path: Path) -> None:
    code = """
import asyncio
import sys
from sqlalchemy import event

sys.path.insert(0, 'apps/web-server')
from server.database.engine import engine, init_db

@event.listens_for(engine.sync_engine, 'before_cursor_execute')
def print_statement(conn, cursor, statement, parameters, context, executemany):
    print(statement)

asyncio.run(init_db())
"""
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}",
            "APP_MIGRATIONS_AUTO_APPLY": "true",
            "APP_DISABLE_AUTH": "true",
            "GRAPHITI_ENABLED": "false",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "PRAGMA journal_mode" in result.stdout
    assert "pg_advisory" not in result.stdout.lower()


def _fake_lock_engine(calls: list[str], fail_unlock: bool = False):
    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def execution_options(self, **kwargs):
            self.options = kwargs
            return self

        async def execute(self, statement, parameters=None):
            sql = str(statement)
            calls.append(sql)
            if fail_unlock and "pg_advisory_unlock" in sql:
                raise SQLAlchemyError("unlock failed")

    class Engine:
        def __init__(self):
            self.connection = Connection()

        def connect(self):
            return self.connection

        async def dispose(self):
            calls.append("dispose")

    return Engine()


def test_postgres_upgrade_runs_inside_lock_with_timeout(monkeypatch) -> None:
    calls = []
    fake_engine = _fake_lock_engine(calls)
    create_kwargs = {}

    def create_engine(url, **kwargs):
        create_kwargs.update(kwargs)
        return fake_engine

    monkeypatch.setattr(engine_mod, "create_async_engine", create_engine)
    monkeypatch.setattr(
        engine_mod, "_alembic_upgrade_head_sync", lambda: calls.append("upgrade")
    )

    asyncio.run(engine_mod._upgrade_head_with_pg_lock())

    assert create_kwargs["poolclass"] is NullPool
    assert fake_engine.connection.options == {"isolation_level": "AUTOCOMMIT"}
    assert calls == [
        "SET lock_timeout = '300s'",
        "SELECT pg_advisory_lock(:k)",
        "upgrade",
        "SELECT pg_advisory_unlock(:k)",
        "dispose",
    ]


def test_unlock_failure_is_logged_not_raised(monkeypatch, caplog) -> None:
    calls = []
    fake_engine = _fake_lock_engine(calls, fail_unlock=True)

    def upgrade():
        raise RuntimeError("boom")

    monkeypatch.setattr(
        engine_mod, "create_async_engine", lambda url, **kwargs: fake_engine
    )
    monkeypatch.setattr(engine_mod, "_alembic_upgrade_head_sync", upgrade)

    with pytest.raises(RuntimeError, match="boom"), caplog.at_level("WARNING"):
        asyncio.run(engine_mod._upgrade_head_with_pg_lock())

    assert any(
        record.levelname == "WARNING" and "lock" in record.message.lower()
        for record in caplog.records
    )
    assert calls[-1] == "dispose"
