"""P1.1 — driver selection from DATABASE_URL scheme."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make the web-server source importable for engine inspection.
_WEB_SERVER = Path(__file__).resolve().parents[2] / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))


@pytest.mark.postgres
@pytest.mark.slow
def test_engine_uses_asyncpg_for_postgres_url(test_postgres_url: str) -> None:
    """P1.1 — when DATABASE_URL is postgresql+asyncpg://..., the engine binds asyncpg."""
    with patch.dict(os.environ, {"DATABASE_URL": test_postgres_url}, clear=False):
        # Force re-import to pick up new env
        if "server.database.engine" in sys.modules:
            del sys.modules["server.database.engine"]
        from server.database import engine as engine_module

        assert "asyncpg" in str(engine_module.engine.url), \
            f"engine URL did not select asyncpg: {engine_module.engine.url}"
        assert engine_module.engine.dialect.name == "postgresql", \
            f"dialect is {engine_module.engine.dialect.name}, expected postgresql"


@pytest.mark.postgres
def test_engine_keeps_aiosqlite_for_sqlite_url(tmp_path: Path) -> None:
    """P1.1 — when DATABASE_URL is unset (default), engine uses aiosqlite."""
    sqlite_path = tmp_path / "test.db"
    url = f"sqlite+aiosqlite:///{sqlite_path}"
    with patch.dict(os.environ, {"DATABASE_URL": url}, clear=False):
        if "server.database.engine" in sys.modules:
            del sys.modules["server.database.engine"]
        from server.database import engine as engine_module

        assert engine_module.engine.dialect.name == "sqlite"


@pytest.mark.postgres
def test_wal_listener_skipped_for_postgres(test_postgres_url: str) -> None:
    """P1.1 — the WAL-mode `connect` event hook must NOT fire on a Postgres engine."""
    with patch.dict(os.environ, {"DATABASE_URL": test_postgres_url}, clear=False):
        if "server.database.engine" in sys.modules:
            del sys.modules["server.database.engine"]
        from server.database import engine as engine_module

        # The engine should NOT have the _enable_wal_mode listener registered
        # for a Postgres dialect.
        from sqlalchemy import event

        listeners = event.contains(engine_module.engine.sync_engine, "connect",
                                    engine_module._enable_wal_mode) \
            if hasattr(engine_module, "_enable_wal_mode") else False
        assert not listeners, "WAL listener registered on Postgres engine"
