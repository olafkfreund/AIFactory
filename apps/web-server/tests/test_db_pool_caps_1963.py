"""Pool cap defaults and overrides for the web and teardown engines (#1963)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

_WEB_SERVER = Path(__file__).resolve().parents[1]
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.db_pool import pool_kwargs  # noqa: E402

URL = "postgresql+asyncpg://u:p@127.0.0.1:1/db"


@pytest.fixture(autouse=True)
def _clear_pool_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "TEARDOWN_DB_POOL_SIZE",
        "TEARDOWN_DB_MAX_OVERFLOW",
        "DB_POOL_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_pool_kwargs_defaults() -> None:
    assert pool_kwargs(URL, "DB", 2, 2) == {
        "pool_size": 2,
        "max_overflow": 2,
        "pool_timeout": 10,
    }
    assert pool_kwargs(URL, "TEARDOWN_DB", 1, 0) == {
        "pool_size": 1,
        "max_overflow": 0,
        "pool_timeout": 10,
    }


def test_pool_kwargs_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEARDOWN_DB_POOL_SIZE", "3")
    monkeypatch.setenv("TEARDOWN_DB_MAX_OVERFLOW", "4")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "5")
    assert pool_kwargs(URL, "TEARDOWN_DB", 1, 0) == {
        "pool_size": 3,
        "max_overflow": 4,
        "pool_timeout": 5,
    }


def test_sqlite_ignores_pool_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "invalid")
    assert pool_kwargs("sqlite+aiosqlite:///test.db", "DB", 2, 2) == {}


def test_pool_kwargs_rejects_non_integer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "invalid")
    with pytest.raises(ValueError):
        pool_kwargs(URL, "DB", 2, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prefix", "size", "overflow"),
    [("DB", 2, 2), ("TEARDOWN_DB", 1, 0)],
)
async def test_engine_pool_caps(prefix: str, size: int, overflow: int) -> None:
    engine = create_async_engine(URL, **pool_kwargs(URL, prefix, size, overflow))
    try:
        assert isinstance(engine.pool, AsyncAdaptedQueuePool)
        assert engine.pool.size() == size
        assert engine.pool._max_overflow == overflow
        assert engine.pool._timeout == 10
    finally:
        await engine.dispose()
